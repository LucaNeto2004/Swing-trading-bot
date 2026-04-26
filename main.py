"""
Swing Trading Bot — main loop.

Strategy: whale swing (58bro + nervousdegen informed).
Pipeline: fetch candles → features → strategy per symbol → risk gate → execution.

Paper trading by default. Live trading requires explicit unlock (paper_trading=False).
Runs all symbols in config.instruments — max_concurrent_positions caps concurrency.
"""
import json
import os
import signal as sig
import sys
import threading
import time
from datetime import datetime, timezone

import requests

from config.settings import load_config, is_tradeable_now
from config.deployer import load_all
from core.alerts import AlertManager
from core.data import DataManager
from core.execution import PaperTrader
from core.journal import write_daily_journal
from core.risk import RiskGate
from core.scans import detect_scan_triggers, score_trigger
from core.strategy_distance import strategy_distance
from strategies.base import SignalType
from strategies.whale_swing import WhaleSwingConfig, WhaleSwingStrategy
from utils.logger import setup_logger

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
PRICE_POLL_INTERVAL_SECONDS = 5

log = setup_logger("main")

print("\033]0;swing-bot\007", end="", flush=True)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(_BASE_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, "data"), exist_ok=True)

# Rolling signals feed consumed by the v2 /signals tab. Each line is one
# event emitted when a symbol's strategy.evaluate() returns a signal. Kept
# capped at SIGNALS_MAX to prevent unbounded growth.
SIGNALS_FILE = os.path.join(_BASE_DIR, "logs", "signals.jsonl")
SIGNALS_MAX = 500


def _append_signal(ev: dict) -> None:
    try:
        with open(SIGNALS_FILE, "a") as f:
            f.write(json.dumps(ev, default=str) + "\n")
        # Trim if file grew past cap (cheap — read+rewrite infrequently).
        try:
            with open(SIGNALS_FILE) as f:
                lines = f.readlines()
            if len(lines) > SIGNALS_MAX * 2:
                with open(SIGNALS_FILE, "w") as f:
                    f.writelines(lines[-SIGNALS_MAX:])
        except Exception:
            pass
    except Exception as e:
        log.debug(f"signals emit: {e}")


class PriceMonitor:
    """Background poller that fires SL/TP1/trail on live mark price, not just
    at 5m candle close. Uses HL's `allMids` endpoint — one cheap request per
    interval regardless of how many positions are open."""

    def __init__(self, paper: PaperTrader, risk: RiskGate, alerts: AlertManager,
                 data: "DataManager", strategies: dict,
                 interval: int = PRICE_POLL_INTERVAL_SECONDS):
        self.paper = paper
        self.risk = risk
        self.alerts = alerts
        self.data = data
        self.strategies = strategies
        self.interval = interval
        self.running = False
        self._thread: threading.Thread | None = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="price-monitor")
        self._thread.start()
        log.info(f"Price monitor started — polling every {self.interval}s")

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                if self.paper.open_count() > 0:
                    self._check_open_positions()
            except Exception as e:
                log.warning(f"price monitor error: {e}")
            # sleep in small chunks so shutdown is responsive
            for _ in range(self.interval):
                if not self.running:
                    return
                time.sleep(1)

    def _check_open_positions(self):
        mids = self._fetch_mids()
        if not mids:
            return
        for sym in list(self.paper.positions.keys()):
            price = mids.get(sym)
            if price is None or price <= 0:
                continue
            # Provide BOS pivot levels + regime state for positions using
            # non-standard exit types. Harmless for standard-exit positions.
            pos = self.paper.positions.get(sym)
            bos_h = bos_l = None
            rgm_up = rgm_dn = None
            ph_arr = self.data.last_pivot_h_1h.get(sym)
            pl_arr = self.data.last_pivot_l_1h.get(sym)
            if ph_arr is not None and len(ph_arr) and not (ph_arr[-1] != ph_arr[-1]):
                bos_h = float(ph_arr[-1])
            if pl_arr is not None and len(pl_arr) and not (pl_arr[-1] != pl_arr[-1]):
                bos_l = float(pl_arr[-1])
            if pos is not None and pos.exit_type == "regime_flip":
                strat = self.strategies.get(sym)
                fv = getattr(strat.cfg, "trend_filter_1h", "ema_cross") if strat else "ema_cross"
                rgm_up, rgm_dn = self.data.latest_1h_regime(sym, fv)
            ens_up = ens_dn = None
            if pos is not None and pos.exit_type == "ensemble_hybrid":
                ens_up, ens_dn, _, _ = self.data.latest_ensemble_counts(sym)
            piv_h_evt = piv_l_evt = None
            rgm_label = None
            if pos is not None and pos.exit_type == "pullback_exit":
                piv_h_evt, piv_l_evt = self.data.latest_pivot_event(sym)
                rgm_label = self.data.latest_regime_label(sym)
            trades = self.paper.intrabar_check(sym, price,
                                               bos_pivot_h=bos_h, bos_pivot_l=bos_l,
                                               regime_up=rgm_up, regime_dn=rgm_dn,
                                               ens_up_cnt=ens_up, ens_dn_cnt=ens_dn,
                                               pivot_h_event=piv_h_evt, pivot_l_event=piv_l_evt,
                                               regime_label=rgm_label)
            for t in trades:
                self.risk.record_trade(t.pnl or 0.0, t.symbol)
                self.alerts.send_exit(t.symbol, t.side, t.price,
                                      t.pnl or 0.0, t.exit_reason, t.held_bars)

    @staticmethod
    def _fetch_mids() -> dict[str, float]:
        try:
            r = requests.post(HL_INFO_URL, json={"type": "allMids"}, timeout=8)
            if r.status_code != 200:
                return {}
            data = r.json() or {}
            return {k: float(v) for k, v in data.items()}
        except Exception:
            return {}


class SwingBot:
    def __init__(self):
        self.config = load_config()
        self.running = False
        self.data = DataManager(self.config)
        self.risk = RiskGate(self.config)
        self.paper = PaperTrader(self.config)
        self.alerts = AlertManager(self.config)

        # Build strategies FIRST so PriceMonitor receives a populated dict.
        deployed = load_all()
        self.strategies: dict[str, WhaleSwingStrategy] = {}
        self.cfg_labels: dict[str, str] = {}
        for sym in self.config.instruments.keys():
            dep = deployed.get(sym)
            if not dep:
                log.warning(f"no deployed config for {sym} — skipping")
                continue
            cfg = WhaleSwingConfig.from_json(dep)
            self.strategies[sym] = WhaleSwingStrategy(cfg)
            self.cfg_labels[sym] = dep.get("config", "")

        self.price_monitor = PriceMonitor(self.paper, self.risk, self.alerts,
                                          self.data, self.strategies)

        # UTC date of the day currently in flight. Day-roll triggers a journal
        # write for the previous day at the top of the next cycle.
        self._journal_date = datetime.now(timezone.utc).date()

    def start(self):
        self.running = True
        sig.signal(sig.SIGINT, self._shutdown)
        sig.signal(sig.SIGTERM, self._shutdown)
        mode = "PAPER" if self.config.paper_trading else "LIVE"
        net = "TESTNET" if self.config.testnet else "MAINNET"
        sz = self.config.sizing
        log.info("=" * 60)
        log.info(f"  Swing Trading Bot — Whale Strategy")
        log.info(f"  Mode: {mode} | Network: {net}")
        log.info(f"  Symbols: {list(self.strategies.keys())}")
        log.info(f"  Sizing: margin {sz.margin_pct*100:.0f}% × lev {sz.set_leverage}x → "
                 f"{sz.effective_leverage:.1f}× effective, liq ~{sz.liquidation_distance_pct:.0f}% away")
        log.info(f"  Max concurrent: {self.config.risk.max_concurrent_positions}")
        btc_confirm_syms = [
            s for s, st in self.strategies.items()
            if getattr(st.cfg, "require_btc_1h_confirm", False)
        ]
        if btc_confirm_syms:
            log.info(f"  BTC-1h-confirm ON for: {btc_confirm_syms}")
        log.info("=" * 60)
        self.alerts.send_status("online", f"Swing bot {mode} on {net} — {len(self.strategies)} symbols")

        self.price_monitor.start()

        cycle = 0
        log.info("Waiting for next 5m candle close to align cycles...")
        while self.running:
            self._wait_for_next_5m_candle()
            if not self.running:
                break
            try:
                cycle += 1
                log.info(f"--- Cycle {cycle} | {datetime.now().strftime('%H:%M:%S')} ---")
                self._run_cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)
                self.alerts.send_status("error", str(e))
        self._on_shutdown()

    def _wait_for_next_5m_candle(self):
        """Sleep until ~5s past the next 5m candle close."""
        CANDLE_SECONDS = 300
        BUFFER = 5
        while self.running:
            now = datetime.now()
            seconds_past = (now.minute % 5) * 60 + now.second
            remaining = CANDLE_SECONDS - seconds_past + BUFFER
            if remaining >= CANDLE_SECONDS:
                remaining -= CANDLE_SECONDS
            if remaining <= 1:
                time.sleep(max(1, remaining))
                return
            # Sleep in chunks so SIGINT is responsive
            time.sleep(min(remaining, 5))

    def _run_cycle(self):
        # UTC day-roll → write yesterday's journal to logs/daily/
        try:
            today_utc = datetime.now(timezone.utc).date()
            if today_utc != self._journal_date:
                prev = self._journal_date
                self._journal_date = today_utc
                out = write_daily_journal(
                    prev,
                    os.path.join(_BASE_DIR, "data", "paper_state.json"),
                    os.path.join(_BASE_DIR, "logs", "daily"),
                )
                if out:
                    log.info(f"Daily journal written: {out}")
        except Exception as e:
            log.warning(f"daily journal failed: {e}")

        # Update portfolio state
        self.risk.update_portfolio(self.paper.get_balance(), list(self.paper.positions.keys()))
        if self.risk.kill_switch or self.risk.account_dd_halt or self.risk.consecutive_loss_halt:
            log.warning("Risk halt active — skipping signals")
            return

        for sym, strategy in self.strategies.items():
            try:
                is_new = self.data.refresh(sym)
                df5 = self.data.df_5m.get(sym)
                if df5 is None or df5.empty or len(df5) < 55:
                    continue

                # SL/TP1/trail are checked every cycle, but bars_held only
                # advances on a NEW 5m bar close. Use tick() when a bar has
                # just closed (is_new=True) so bars_held increments by 1;
                # otherwise use intrabar_check() which runs SL/TP1/trail at
                # the live close without touching bars_held.
                latest = df5.iloc[-1]
                # Current ICT state for CHoCH-exit detection inside tick(). If
                # the symbol isn't in the structure dict yet (first fetch),
                # fall back to None = neutral so tick() doesn't force-close.
                up_s_arr = self.data.up_struct_1h.get(sym)
                dn_s_arr = self.data.dn_struct_1h.get(sym)
                up_struct_latest = bool(up_s_arr[-1]) if up_s_arr is not None and len(up_s_arr) else None
                dn_struct_latest = bool(dn_s_arr[-1]) if dn_s_arr is not None and len(dn_s_arr) else None
                # BOS / regime context for exit logic (harmless for standard exits)
                _pos = self.paper.positions.get(sym)
                _bos_h = _bos_l = None
                _rgm_up = _rgm_dn = None
                _ph_arr = self.data.last_pivot_h_1h.get(sym)
                _pl_arr = self.data.last_pivot_l_1h.get(sym)
                if _ph_arr is not None and len(_ph_arr) and not (_ph_arr[-1] != _ph_arr[-1]):
                    _bos_h = float(_ph_arr[-1])
                if _pl_arr is not None and len(_pl_arr) and not (_pl_arr[-1] != _pl_arr[-1]):
                    _bos_l = float(_pl_arr[-1])
                if _pos is not None and _pos.exit_type == "regime_flip":
                    _fv = getattr(strategy.cfg, "trend_filter_1h", "ema_cross")
                    _rgm_up, _rgm_dn = self.data.latest_1h_regime(sym, _fv)
                _ens_up = _ens_dn = None
                if _pos is not None and _pos.exit_type == "ensemble_hybrid":
                    _ens_up, _ens_dn, _, _ = self.data.latest_ensemble_counts(sym)
                _piv_h_evt = _piv_l_evt = None
                _rgm_label = None
                if _pos is not None and _pos.exit_type == "pullback_exit":
                    _piv_h_evt, _piv_l_evt = self.data.latest_pivot_event(sym)
                    _rgm_label = self.data.latest_regime_label(sym)

                if is_new:
                    trades = self.paper.tick(
                        sym,
                        high=float(latest['high']),
                        low=float(latest['low']),
                        close=float(latest['close']),
                        up_struct=up_struct_latest,
                        dn_struct=dn_struct_latest,
                        bos_pivot_h=_bos_h, bos_pivot_l=_bos_l,
                        regime_up=_rgm_up, regime_dn=_rgm_dn,
                        ens_up_cnt=_ens_up, ens_dn_cnt=_ens_dn,
                        pivot_h_event=_piv_h_evt, pivot_l_event=_piv_l_evt,
                        regime_label=_rgm_label,
                    )
                else:
                    trades = self.paper.intrabar_check(
                        sym, float(latest['close']),
                        bos_pivot_h=_bos_h, bos_pivot_l=_bos_l,
                        regime_up=_rgm_up, regime_dn=_rgm_dn,
                        ens_up_cnt=_ens_up, ens_dn_cnt=_ens_dn,
                        pivot_h_event=_piv_h_evt, pivot_l_event=_piv_l_evt,
                        regime_label=_rgm_label,
                    )
                for t in trades:
                    self.risk.record_trade(t.pnl or 0.0, t.symbol)
                    self.alerts.send_exit(t.symbol, t.side, t.price,
                                          t.pnl or 0.0, t.exit_reason, t.held_bars)

                # Only evaluate NEW entry signals on new 5m bar close
                if not is_new:
                    continue

                # -- SCAN TRIGGERS → signals feed --
                # Emit telemetry for interesting-bar events (bb_touch, ema_cross,
                # vol_spike, etc.) even when no strategy fires. Runs before the
                # position/tradeable gates so watch rows surface for symbols we
                # already hold. Edge-only, so output stays sparse.
                try:
                    triggers = detect_scan_triggers(df5)
                except Exception as e:
                    triggers = []
                    log.debug(f"{sym}: scan error — {e}")
                if triggers:
                    _ens_up_s, _ens_dn_s, _ens_up_prev_s, _ens_dn_prev_s = self.data.latest_ensemble_counts(sym)
                    _reg_s = self.data.latest_regime_label(sym)
                    _last = df5.iloc[-1]
                    _rsi_s = float(_last.get("rsi", 0) or 0)
                    _close_s = float(_last["close"])
                    _ts_s = datetime.now(timezone.utc).isoformat()
                    _has_pos = sym in self.paper.positions
                    # High-confidence scans get promoted to PRIMED — these are
                    # the ones with strong 1h consensus + matching regime. The
                    # threshold (0.65) mirrors the mockup's density (only a
                    # handful of primes per session).
                    PRIMED_THRESH = 0.65
                    for _trig, _side in triggers:
                        _conf = score_trigger(_trig, _side, _ens_up_s, _ens_dn_s, _reg_s, _rsi_s)
                        _is_primed = _conf >= PRIMED_THRESH
                        _dist = strategy_distance(
                            entry_type=getattr(strategy.cfg, "entry_type", ""),
                            side=_side,
                            cfg=strategy.cfg,
                            ens_up=_ens_up_s, ens_dn=_ens_dn_s,
                            ens_up_prev=_ens_up_prev_s, ens_dn_prev=_ens_dn_prev_s,
                            regime=_reg_s,
                            has_open_position=_has_pos,
                        )
                        _append_signal({
                            "id": f"scan_{int(time.time()*1000)}_{sym.lower()}_{_trig}_{_side.lower()}",
                            "ts": _ts_s,
                            "symbol": sym,
                            "side": _side,
                            "strategy": "scan",
                            "entry_type": _trig,
                            "timeframe": "5m",
                            "confidence": _conf,
                            "price": _close_s,
                            "entry": _close_s,
                            "rsi": round(_rsi_s, 1),
                            "ens_up": int(_ens_up_s or 0),
                            "ens_dn": int(_ens_dn_s or 0),
                            "regime": _reg_s or "",
                            "strategy_entry": getattr(strategy.cfg, "entry_type", ""),
                            "strategy_distance": _dist,
                            "status": "primed" if _is_primed else "watch",
                            "action": f"PRIMED {_side}" if _is_primed else "WATCH",
                        })

                # -- PYRAMID ADD check (runs only when the symbol already has an
                #    open position + all live-tight gates pass) --
                if sym in self.paper.positions:
                    self._maybe_pyramid_add(sym, df5)
                    continue

                # Per-symbol trading-hours window (e.g. xyz:SILVER London+NY weekdays only)
                tradeable, window_reason = is_tradeable_now(sym)
                if not tradeable:
                    continue

                # 1h filter + optional 4h HTF gate. Dispatch lives in DataManager.
                filter_variant = getattr(strategy.cfg, "trend_filter_1h", "ema_cross")
                up_1h, dn_1h = self.data.latest_1h_regime(sym, filter_variant)
                if getattr(strategy.cfg, "require_4h_agreement", False):
                    up_4h, dn_4h = self.data.latest_4h_regime(sym)
                    up_1h = up_1h and up_4h
                    dn_1h = dn_1h and dn_4h
                # Extras for BOS + regime_flip entry types
                ph_arr = self.data.last_pivot_h_1h.get(sym)
                pl_arr = self.data.last_pivot_l_1h.get(sym)
                last_ph = float(ph_arr[-1]) if ph_arr is not None and len(ph_arr) and not (ph_arr[-1] != ph_arr[-1]) else None
                last_pl = float(pl_arr[-1]) if pl_arr is not None and len(pl_arr) and not (pl_arr[-1] != pl_arr[-1]) else None
                # Previous-bar filter state for regime_flip transition detection
                up_1h_prev, dn_1h_prev = None, None
                if len(df5) >= 2:
                    prev_bar_ts = df5['timestamp'].iloc[-2]
                    # Re-derive at prev index from the stored arrays
                    def _prev(arr):
                        return bool(arr[-2]) if arr is not None and len(arr) >= 2 else None
                    if filter_variant == "structure":
                        up_1h_prev = _prev(self.data.up_struct_1h.get(sym))
                        dn_1h_prev = _prev(self.data.dn_struct_1h.get(sym))
                    elif filter_variant == "both_agree":
                        u_e = _prev(self.data.up_1h.get(sym)); u_s = _prev(self.data.up_struct_1h.get(sym))
                        d_e = _prev(self.data.dn_1h.get(sym)); d_s = _prev(self.data.dn_struct_1h.get(sym))
                        up_1h_prev = (u_e and u_s) if (u_e is not None and u_s is not None) else None
                        dn_1h_prev = (d_e and d_s) if (d_e is not None and d_s is not None) else None
                    elif filter_variant == "hma_slope":
                        up_1h_prev = _prev(self.data.up_hma_1h.get(sym))
                        dn_1h_prev = _prev(self.data.dn_hma_1h.get(sym))
                    elif filter_variant == "sjm":
                        up_1h_prev = _prev(self.data.up_sjm_1h.get(sym))
                        dn_1h_prev = _prev(self.data.dn_sjm_1h.get(sym))
                    elif filter_variant == "kalman":
                        up_1h_prev = _prev(self.data.up_kalman_1h.get(sym))
                        dn_1h_prev = _prev(self.data.dn_kalman_1h.get(sym))
                    else:
                        up_1h_prev = _prev(self.data.up_1h.get(sym))
                        dn_1h_prev = _prev(self.data.dn_1h.get(sym))
                ens_up_cnt, ens_dn_cnt, ens_up_prev, ens_dn_prev = self.data.latest_ensemble_counts(sym)
                reg_label = self.data.latest_regime_label(sym)
                piv_h_evt, piv_l_evt = self.data.latest_pivot_event(sym)
                signal = strategy.evaluate(sym, df5, up_1h, dn_1h,
                                           last_pivot_h=last_ph, last_pivot_l=last_pl,
                                           up_1h_prev=up_1h_prev, dn_1h_prev=dn_1h_prev,
                                           ens_up_cnt=ens_up_cnt, ens_dn_cnt=ens_dn_cnt,
                                           ens_up_cnt_prev=ens_up_prev, ens_dn_cnt_prev=ens_dn_prev,
                                           regime_label=reg_label,
                                           pivot_h_event=piv_h_evt, pivot_l_event=piv_l_evt)
                if signal is None:
                    continue

                # Build a baseline signal-event record we can enrich with the
                # eventual outcome (filled / skip-btc / skip-risk).
                latest_bar = df5.iloc[-1]
                entry_px = float(signal.entry_price)
                atr = float(signal.atr) if signal.atr else 0.0
                is_long = signal.signal_type.value == "long"
                cfg = strategy.cfg
                # Estimate SL/TP1 using strategy cfg so skipped-signal rows
                # still carry the same levels the trade would've used.
                if getattr(cfg, "entry_type", "") in ("test_bounce", "pullback_in_regime"):
                    sl_est = entry_px * (0.97 if is_long else 1.03)
                else:
                    sl_est = entry_px - atr * cfg.sl_atr if is_long else entry_px + atr * cfg.sl_atr
                tp1_est = (entry_px + atr * cfg.tp1_atr) if is_long else (entry_px - atr * cfg.tp1_atr)
                risk = abs(entry_px - sl_est)
                reward = abs(tp1_est - entry_px)
                rr = round(reward / risk, 2) if risk > 0 else None
                ens_total = 5
                conf = max((ens_up_cnt or 0), (ens_dn_cnt or 0)) / ens_total

                rsi_val = float(latest_bar.get("rsi", 0) or 0)
                reasoning = [
                    f"entry_type={cfg.entry_type}",
                    f"RSI {rsi_val:.1f}",
                    f"regime={reg_label or '?'}",
                    f"ensemble up/dn {ens_up_cnt or 0}/{ens_dn_cnt or 0}",
                ]
                if piv_h_evt:
                    reasoning.append("1h pivot_H just confirmed")
                if piv_l_evt:
                    reasoning.append("1h pivot_L just confirmed")
                if getattr(signal, "reason", ""):
                    reasoning.append(signal.reason)

                ts_iso = datetime.now(timezone.utc).isoformat()
                sig_event = {
                    "id": f"sig_{int(time.time()*1000)}_{sym.lower()}",
                    "ts": ts_iso,
                    "symbol": sym,
                    "side": "LONG" if is_long else "SHORT",
                    "strategy": "whale-swing",
                    "entry_type": getattr(cfg, "entry_type", ""),
                    "timeframe": "5m",
                    "confidence": round(conf, 3),
                    "price": entry_px,
                    "entry": entry_px,
                    "stop": round(sl_est, 6),
                    "target": round(tp1_est, 6),
                    "rr": rr,
                    "rsi": rsi_val,
                    "ens_up": int(ens_up_cnt or 0),
                    "ens_dn": int(ens_dn_cnt or 0),
                    "regime": reg_label or "",
                    "reasoning": reasoning,
                }

                # BTC 1h-confirm gate (opt-in per symbol).
                if getattr(strategy.cfg, "require_btc_1h_confirm", False) and sym != "BTC":
                    want = 1 if signal.signal_type.value == "long" else -1
                    if self.data.btc_1h_dir != want:
                        log.info(
                            f"{sym} {signal.signal_type.value}: skipped — "
                            f"BTC 1h dir={self.data.btc_1h_dir}, need={want}"
                        )
                        _append_signal({**sig_event, "status": "skip",
                                        "action": f"SKIP · BTC 1h={self.data.btc_1h_dir}"})
                        continue

                passed, reason = self.risk.check(signal)
                if not passed:
                    log.info(f"REJECTED {sym} {signal.signal_type.value}: {reason}")
                    _append_signal({**sig_event, "status": "skip",
                                    "action": f"SKIP · {reason}"})
                    continue

                pos = self.paper.open(signal, strategy.cfg, self.cfg_labels.get(sym, ""))
                if pos is not None:
                    self.alerts.send_entry(
                        symbol=sym,
                        side=pos.side,
                        price=pos.entry_price,
                        reason=signal.reason,
                        size=pos.size,
                        notional=pos.notional,
                        sl=pos.sl,
                        tp1=pos.tp1,
                    )
                    _eff_lev = pos.notional / max(self.paper.balance, 1)
                    _append_signal({**sig_event, "status": "filled",
                                    "action": f"OPEN {pos.side.upper()} · {_eff_lev:.1f}×",
                                    "size": float(pos.size),
                                    "notional": float(pos.notional)})
                else:
                    _append_signal({**sig_event, "status": "skip",
                                    "action": "SKIP · already open"})

            except Exception as e:
                log.error(f"{sym}: cycle error — {e}", exc_info=True)

    def _maybe_pyramid_add(self, sym, df5):
        """Check pyramid-add conditions for an existing position on this 5m bar.

        All gates must pass:
          - pyramid config enabled + symbol not banned
          - position has already hit TP1 (proven winner)
          - this 1h bar just printed a fresh BOS in the trade's direction
          - MFE (max favorable excursion) >= min_mfe_atr
          - hours since last action on this position >= min_hours_since_last_action
          - portfolio leverage (total notional / equity) < portfolio_leverage_cap
          - account drawdown from peak < drawdown_lock_pct
        """
        pyr = self.config.pyramid
        if not pyr.enabled:
            return
        if sym in pyr.banned_symbols:
            return
        pos = self.paper.positions.get(sym)
        if pos is None or pos.n_pyramid_adds >= pyr.max_adds:
            return
        if pyr.require_tp1_hit and not pos.tp1_hit:
            return
        if pos.max_favorable_atr < pyr.min_mfe_atr:
            return

        # Fresh BOS on this bar in the trade's direction?
        bos_src = self.data.bos_up_1h.get(sym) if pos.side == "long" else self.data.bos_dn_1h.get(sym)
        if bos_src is None or not bool(bos_src[-1]):
            return

        # Time-since-last-action gate
        try:
            last = datetime.fromisoformat(pos.last_action_ts.replace("+00:00", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            hours_since = (now - last).total_seconds() / 3600
        except Exception:
            hours_since = 0.0
        if hours_since < pyr.min_hours_since_last_action:
            return

        # Drawdown lock — compare current equity to peak
        equity = self.paper.balance
        peak = getattr(self.risk, "account_peak_balance", equity) or equity
        if peak > 0:
            dd_pct = (peak - equity) / peak * 100
            if dd_pct >= pyr.drawdown_lock_pct:
                log.info(f"{sym}: pyramid skipped — account DD {dd_pct:.1f}% >= lock {pyr.drawdown_lock_pct}%")
                return

        # Portfolio leverage cap — would the add breach it?
        total_notional_now = sum(p.notional for p in self.paper.positions.values())
        # Estimate add notional using same formula add_to_position will use
        inst = self.config.instruments.get(sym)
        sym_max_lev = inst.hl_max_leverage if inst is not None else self.config.sizing.set_leverage
        set_lev = min(self.config.sizing.set_leverage, sym_max_lev)
        est_add_notional = equity * pyr.add_margin_pct * set_lev
        projected_lev = (total_notional_now + est_add_notional) / max(equity, 1)
        if projected_lev > pyr.portfolio_leverage_cap:
            log.info(f"{sym}: pyramid skipped — portfolio lev would hit {projected_lev:.2f}x "
                     f"> cap {pyr.portfolio_leverage_cap}x")
            return

        # All gates pass — fire the add
        latest = df5.iloc[-1]
        price = float(latest['close'])
        atr = float(latest['atr'])
        updated = self.paper.add_to_position(
            sym, price, atr, datetime.now(timezone.utc).isoformat()
        )
        if updated is not None:
            self.alerts.send_entry(
                symbol=sym, side=updated.side, price=price,
                reason=f"pyramid add layer {updated.n_pyramid_adds}/{pyr.max_adds} on BOS",
                size=updated.size, notional=updated.notional,
                sl=updated.sl, tp1=updated.tp1,
            )
            # Emit SCALE event to the signals feed
            try:
                _base_mgn = self.config.sizing.margin_pct or 0
                _pct = int(round((pyr.add_margin_pct / _base_mgn) * 100)) if _base_mgn else 0
                _append_signal({
                    "id": f"scale_{int(time.time()*1000)}_{sym.lower()}",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": sym,
                    "side": "LONG" if updated.side == "long" else "SHORT",
                    "strategy": "whale-swing",
                    "entry_type": "pyramid_add",
                    "timeframe": "5m",
                    "confidence": 0.8,
                    "price": price,
                    "entry": price,
                    "rsi": round(float(latest.get("rsi", 0) or 0), 1),
                    "regime": self.data.latest_regime_label(sym) or "",
                    "status": "filled",
                    "action": f"SCALE +{_pct}%",
                })
            except Exception as e:
                log.debug(f"{sym}: scale emit error — {e}")

    def _shutdown(self, signum=None, frame=None):
        log.info("Shutdown signal received")
        self.running = False

    def _on_shutdown(self):
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        try:
            self.price_monitor.stop()
        except Exception:
            pass
        try:
            self.risk.save_state()
            self.paper._save_state()
        except Exception as e:
            log.error(f"Shutdown save failed: {e}")
        try:
            self.alerts.send_status("offline", "Swing bot stopped")
            self.alerts.flush()
        except Exception:
            pass
        log.info("Goodbye.")


def main():
    restart_count = 0
    MAX_RESTARTS = 10
    while restart_count < MAX_RESTARTS:
        try:
            bot = SwingBot()
            bot.start()
            break
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            restart_count += 1
            wait = min(30, 5 * restart_count)
            log.error(f"Bot crashed: {e} — restart {restart_count}/{MAX_RESTARTS} in {wait}s",
                      exc_info=True)
            time.sleep(wait)
    if restart_count >= MAX_RESTARTS:
        log.error("Max restarts reached — exiting")
        sys.exit(1)


if __name__ == "__main__":
    main()
