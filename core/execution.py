"""
Paper execution engine. Manages open positions with:
  - Stop loss (cfg.sl_atr × ATR at entry)
  - Partial TP1 (close cfg.tp1_pct at cfg.tp1_atr × ATR)
  - SL moves to breakeven after TP1 hit
  - Trailing stop (cfg.trail_atr × ATR) — only activates after price moves 1× offset
  - Max hold (cfg.max_hold_bars) — force close if reached

Live trading is a stub — requires explicit human unlock before it runs.
Obsidian trade notes are written via shared/vault_writer.py.
"""
import json
import os
import sys as _sys
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from collections import deque
from config.settings import BotConfig, INSTRUMENTS
from strategies.base import EntrySignal, SignalType
from strategies.whale_swing import WhaleSwingConfig
from utils.logger import setup_logger

STRUCTURAL_LOOKBACK_BARS = 10  # ~50m on 5m — recent swing window for runner SL

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_STATE_FILE = os.path.join(_BASE_DIR, "data", "paper_state.json")

_SHARED_DIR = os.path.abspath(os.path.join(_BASE_DIR, "..", "shared"))
if _SHARED_DIR not in _sys.path:
    _sys.path.insert(0, _SHARED_DIR)
try:
    import vault_writer  # type: ignore
except Exception:
    vault_writer = None  # type: ignore

log = setup_logger("execution")


@dataclass
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str               # "long" | "short"
    size: float             # units of base asset
    price: float            # fill price (exit)
    notional: float         # size × exit price
    pnl: Optional[float] = None
    exit_reason: str = ""   # "stop_loss" | "tp1_partial" | "trail_stop" | "max_hold"
    held_bars: int = 0
    runner_r: Optional[float] = None                 # price-P&L / initial-risk (runner's final exit only)
    favorable_excursion_atr: Optional[float] = None  # max in-favor excursion in ATR multiples
    # --- trade-analytics fields (2026-04-23) ---
    entry_price: Optional[float] = None              # original entry fill price
    initial_sl: Optional[float] = None               # original SL (before BE move) — for R calc
    r: Optional[float] = None                        # P&L(price) / initial-risk, on ALL exits (not just runner)
    adverse_excursion_atr: Optional[float] = None    # max against-trade excursion in ATR multiples (MAE)


@dataclass
class OpenPosition:
    symbol: str
    side: str                    # "long" | "short"
    entry_price: float
    size: float
    notional: float
    entry_atr: float             # ATR at entry — used to set SL/TP1/trail offsets
    entry_bar_ts: str            # timestamp of entry bar
    sl: float
    tp1: Optional[float]
    tp1_pct: float
    trail_offset: float          # 0 = no trail
    trail_active: bool = False
    best_price: float = 0.0      # best excursion for trailing
    tp1_hit: bool = False
    # Multi-tier scale-out state — tp2/tp3 prices are computed at entry
    # from cfg.tp2_atr / cfg.tp3_atr × entry_atr. 0 = tier disabled.
    tp2: Optional[float] = None
    tp2_pct: float = 0.0
    tp2_hit: bool = False
    tp3: Optional[float] = None
    tp3_pct: float = 0.0
    tp3_hit: bool = False
    max_hold_bars: int = 288
    bars_held: int = 0
    cfg_label: str = ""          # config label for reporting
    strategy_name: str = "whale_swing"
    entry_fee: float = 0.0       # unamortised entry commission, burned into exit PnLs
    initial_sl: float = 0.0      # preserved original SL for runner_R calc after SL→BE
    max_favorable_atr: float = 0.0  # running max in-favor excursion in ATR multiples
    max_adverse_atr: float = 0.0    # running max against-trade excursion in ATR multiples (MAE)
    set_leverage: int = 40       # asset-specific set lev used to size this position
    # BOS / regime_flip / ensemble exit mode. "standard" = SL/TP/trail/max_hold.
    # "bos_structural" = exit on close past opposing pivot (no SL/TP).
    # "bos_hybrid" = TP1 partial + BOS structural exit.
    # "regime_flip" = exit when single filter turns off trade direction.
    # "ensemble_hybrid" = TP1 partial + exit when 5-filter consensus drops
    #                     below (ensemble_k - 1) for trade direction.
    exit_type: str = "standard"
    # Threshold K captured at entry, for ensemble_hybrid exits. Ignored for
    # other exit types.
    ensemble_k: int = 4
    # Opposite-side pivot at entry — snapshot for test_exit. When a NEW pivot
    # on the opposite side confirms (value changes), the test_exit fires.
    entry_pivot_h: float = 0.0
    entry_pivot_l: float = 0.0
    recent_lows: list = field(default_factory=list)   # rolling window of bar lows (STRUCTURAL_LOOKBACK_BARS)
    recent_highs: list = field(default_factory=list)  # rolling window of bar highs
    # Pyramid state — each successful add increments n_pyramid_adds and updates
    # last_action_ts so the time-since-last-action gate works.
    n_pyramid_adds: int = 0
    last_action_ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OpenPosition":
        # Tolerate older state files that don't have the new fields
        d = dict(d)
        d.setdefault("set_leverage", 40)
        d.setdefault("recent_lows", [])
        d.setdefault("recent_highs", [])
        d.setdefault("n_pyramid_adds", 0)
        d.setdefault("last_action_ts", d.get("entry_bar_ts", ""))
        d.setdefault("tp2", None); d.setdefault("tp2_pct", 0.0); d.setdefault("tp2_hit", False)
        d.setdefault("tp3", None); d.setdefault("tp3_pct", 0.0); d.setdefault("tp3_hit", False)
        d.setdefault("max_adverse_atr", 0.0)
        return cls(**d)


def _trade_r(pos: "OpenPosition", exit_price: float) -> Optional[float]:
    """Risk-multiple (R) for a trade leg. Requires pos.initial_sl set at entry.
    Returns None if initial_sl missing or zero risk distance."""
    if not pos.initial_sl or pos.initial_sl == 0.0 or pos.entry_price == 0.0:
        return None
    risk = abs(pos.entry_price - pos.initial_sl)
    if risk <= 0:
        return None
    if pos.side == "long":
        return (exit_price - pos.entry_price) / risk
    return (pos.entry_price - exit_price) / risk


def _note_trade_safe(trade: dict):
    if vault_writer is None:
        return
    try:
        vault_writer.write_trade_note(trade)
    except Exception as e:
        log.warning(f"vault_writer failed: {e}")


class PaperTrader:
    def __init__(self, config: BotConfig):
        self.config = config
        self.balance: float = config.sizing.account_size
        self.starting_balance: float = config.sizing.account_size
        self.positions: dict[str, OpenPosition] = {}
        self.trade_history: list[TradeRecord] = []
        self._lock = threading.RLock()
        self._load_state()

    def _load_state(self):
        if not os.path.exists(PAPER_STATE_FILE):
            return
        try:
            with open(PAPER_STATE_FILE) as f:
                s = json.load(f)
            self.balance = s.get("balance", self.balance)
            self.starting_balance = s.get("starting_balance", self.balance)
            self.positions = {
                sym: OpenPosition.from_dict(d) for sym, d in s.get("positions", {}).items()
            }
            # Migration: backfill initial_sl for positions saved before that field existed.
            # If TP1 already hit we've lost the original SL distance → runner_r stays None.
            for p in self.positions.values():
                if p.initial_sl == 0.0 and not p.tp1_hit:
                    p.initial_sl = p.sl
            for t in s.get("trade_history", []):
                try:
                    ts = t.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            ts = datetime.fromisoformat(ts)
                        except ValueError:
                            pass
                    self.trade_history.append(TradeRecord(
                        timestamp=ts,
                        symbol=t["symbol"], side=t["side"], size=t["size"],
                        price=t["price"], notional=t["notional"],
                        pnl=t.get("pnl"), exit_reason=t.get("exit_reason", ""),
                        held_bars=t.get("held_bars", 0),
                        runner_r=t.get("runner_r"),
                        favorable_excursion_atr=t.get("favorable_excursion_atr"),
                        entry_price=t.get("entry_price"),
                        initial_sl=t.get("initial_sl"),
                        r=t.get("r"),
                        adverse_excursion_atr=t.get("adverse_excursion_atr"),
                    ))
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"Failed to load paper state: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(PAPER_STATE_FILE), exist_ok=True)
            state = {
                "balance": self.balance,
                "starting_balance": self.starting_balance,
                "positions": {sym: p.to_dict() for sym, p in self.positions.items()},
                "trade_history": [
                    {
                        "timestamp": str(t.timestamp),
                        "symbol": t.symbol, "side": t.side, "size": t.size,
                        "price": t.price, "notional": t.notional, "pnl": t.pnl,
                        "exit_reason": t.exit_reason, "held_bars": t.held_bars,
                        "runner_r": t.runner_r,
                        "favorable_excursion_atr": t.favorable_excursion_atr,
                        "entry_price": t.entry_price,
                        "initial_sl": t.initial_sl,
                        "r": t.r,
                        "adverse_excursion_atr": t.adverse_excursion_atr,
                    }
                    for t in self.trade_history
                ],
                "saved_at": datetime.now().isoformat(),
            }
            tmp = PAPER_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, PAPER_STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save paper state: {e}")

    @staticmethod
    def _compute_bars_held(pos: "OpenPosition") -> int:
        # Derive bars_held from wall-clock (5m since entry_bar_ts) rather than
        # counting tick() calls — the HL candleSnapshot API can lag 5–15s past
        # bar close, so refresh() sometimes skips an is_new=True, which would
        # leave bars_held permanently 1 behind if we relied on tick() alone.
        try:
            entry_dt = pd.Timestamp(pos.entry_bar_ts)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.tz_localize('UTC')
            else:
                entry_dt = entry_dt.tz_convert('UTC')
            now = pd.Timestamp.now(tz='UTC')
            return max(0, int((now - entry_dt).total_seconds() // 300))
        except Exception:
            return pos.bars_held

    def open(self, signal: EntrySignal, cfg: WhaleSwingConfig, cfg_label: str) -> Optional[OpenPosition]:
        with self._lock:
            if signal.symbol in self.positions:
                log.info(f"{signal.symbol}: already has open position, skipping entry")
                return None
            side = "long" if signal.signal_type == SignalType.LONG else "short"
            sizing = self.config.sizing
            # Per-symbol leverage: use HL's max set leverage for this asset,
            # capped at the global sizing.set_leverage (default 40). Keeps
            # 58bro's margin_pct × set_leverage recipe but respects HL caps.
            inst = INSTRUMENTS.get(signal.symbol)
            sym_max_lev = inst.hl_max_leverage if inst is not None else sizing.set_leverage
            set_lev = min(sizing.set_leverage, sym_max_lev)
            margin = self.balance * sizing.margin_pct
            notional = margin * set_lev
            price = signal.entry_price
            size = notional / price
            atr = signal.atr
            # test_bounce + pullback_in_regime use a flat 3% percentage SL
            # (structural-level entries use flat %, not ATR-scaled).
            if cfg.entry_type in ("test_bounce", "pullback_in_regime"):
                TEST_SL_PCT = 0.03
                sl = price * (1 - TEST_SL_PCT) if side == "long" else price * (1 + TEST_SL_PCT)
            else:
                sl = price - atr * cfg.sl_atr if side == "long" else price + atr * cfg.sl_atr
            tp1 = None
            if cfg.tp1_atr > 0:
                tp1 = price + atr * cfg.tp1_atr if side == "long" else price - atr * cfg.tp1_atr
            tp2 = None
            if cfg.tp2_atr > 0:
                tp2 = price + atr * cfg.tp2_atr if side == "long" else price - atr * cfg.tp2_atr
            tp3 = None
            if cfg.tp3_atr > 0:
                tp3 = price + atr * cfg.tp3_atr if side == "long" else price - atr * cfg.tp3_atr
            trail_offset = atr * cfg.trail_atr if cfg.trail_atr > 0 else 0.0
            entry_fee = notional * self.config.risk.commission_pct
            pos = OpenPosition(
                symbol=signal.symbol, side=side, entry_price=price, size=size, notional=notional,
                entry_atr=atr, entry_bar_ts=str(signal.timestamp),
                sl=sl, tp1=tp1, tp1_pct=cfg.tp1_pct, trail_offset=trail_offset,
                best_price=price, max_hold_bars=cfg.max_hold_bars, cfg_label=cfg_label,
                entry_fee=entry_fee, initial_sl=sl, set_leverage=set_lev,
                last_action_ts=str(signal.timestamp),
                tp2=tp2, tp2_pct=cfg.tp2_pct,
                tp3=tp3, tp3_pct=cfg.tp3_pct,
                exit_type=getattr(cfg, "exit_type", "standard"),
                ensemble_k=int(getattr(cfg, "ensemble_k", 4)),
                entry_pivot_h=float(signal.metadata.get("last_pivot_h") or 0.0),
                entry_pivot_l=float(signal.metadata.get("last_pivot_l") or 0.0),
            )
            self.positions[signal.symbol] = pos
            # Entry commission is not deducted from balance here — it's carried on
            # the position and burned into exit PnLs proportionally (TP1 takes its
            # share, final exit takes the rest). End balance is identical; each
            # trade's reported PnL now reflects the full round-trip cost.
            self._save_state()
        log.info(
            f"[PAPER] ENTRY {signal.symbol} {side.upper()} @ ${price:.4f} "
            f"size={size:.4f} notional=${notional:,.2f} lev={set_lev}x "
            f"(eff {sizing.margin_pct * set_lev:.1f}x) SL=${sl:.4f} "
            f"{'TP1=$%.4f' % tp1 if tp1 else 'noTP1'}"
        )
        _note_trade_safe({
            "symbol": signal.symbol, "side": side, "price": price, "size": size,
            "notional": notional, "sl": sl, "tp1": tp1, "reason": signal.reason,
            "timestamp": str(signal.timestamp), "event": "entry",
            "strategy": "whale_swing", "cfg_label": cfg_label,
        })
        return pos

    def add_to_position(self, symbol: str, price: float, atr: float,
                        timestamp_iso: str) -> Optional[OpenPosition]:
        """Pyramid-add onto an existing position on fresh BOS continuation.

        Gating is the caller's responsibility (main.py). This method only
        applies the sizing + weighted-entry update + SL refresh. Returns the
        updated position, or None if the add failed (position missing,
        max_adds reached, or sizing rejected).

        Sizing uses ``sizing.margin_pct`` → ``pyramid.add_margin_pct`` (typically
        ⅓ or ¼ of initial), × per-symbol leverage. The weighted-average entry
        is recomputed on the combined notional.

        SL is refreshed to the most-recent structural level on the rolling
        window (``recent_lows`` for longs / ``recent_highs`` for shorts), so
        the whole stack risks only the current structural invalidation.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return None
            sizing = self.config.sizing
            pyr = self.config.pyramid
            if not pyr.enabled:
                return None
            if pos.n_pyramid_adds >= pyr.max_adds:
                log.info(f"{symbol}: pyramid cap ({pyr.max_adds}) reached, skipping add")
                return None

            inst = INSTRUMENTS.get(symbol)
            sym_max_lev = inst.hl_max_leverage if inst is not None else sizing.set_leverage
            set_lev = min(sizing.set_leverage, sym_max_lev)
            add_margin = self.balance * pyr.add_margin_pct
            add_notional = add_margin * set_lev
            add_size = add_notional / price
            add_fee = add_notional * self.config.risk.commission_pct

            # Weighted-average entry + summed size/notional
            new_size = pos.size + add_size
            new_notional = pos.notional + add_notional
            new_entry = ((pos.entry_price * pos.size) + (price * add_size)) / new_size
            pos.size = new_size
            pos.notional = new_notional
            pos.entry_price = new_entry
            pos.entry_fee += add_fee
            pos.n_pyramid_adds += 1
            pos.last_action_ts = timestamp_iso

            # Refresh SL to the most recent structural level — one combined SL
            # for all layers. On invalidation the whole stack closes together.
            if pos.side == "long" and pos.recent_lows:
                new_sl = min(pos.recent_lows)
            elif pos.side == "short" and pos.recent_highs:
                new_sl = max(pos.recent_highs)
            else:
                new_sl = pos.sl  # keep existing if no structural history yet
            pos.sl = new_sl
            self._save_state()

        log.info(
            f"[PAPER] PYRAMID ADD {symbol} {pos.side.upper()} @ ${price:.4f} "
            f"add_size={add_size:.4f} add_notional=${add_notional:,.2f} "
            f"avg_entry=${pos.entry_price:.4f} total_notional=${pos.notional:,.2f} "
            f"SL→${pos.sl:.4f} layer={pos.n_pyramid_adds}/{self.config.pyramid.max_adds}"
        )
        _note_trade_safe({
            "symbol": symbol, "side": pos.side, "price": price,
            "size": add_size, "notional": add_notional,
            "sl": pos.sl, "timestamp": timestamp_iso,
            "event": "pyramid_add", "strategy": "whale_swing",
            "layer": pos.n_pyramid_adds,
        })
        return pos

    def tick(self, symbol: str, high: float, low: float, close: float,
             up_struct: Optional[bool] = None, dn_struct: Optional[bool] = None,
             bos_pivot_h: Optional[float] = None, bos_pivot_l: Optional[float] = None,
             regime_up: Optional[bool] = None, regime_dn: Optional[bool] = None,
             ens_up_cnt: Optional[int] = None, ens_dn_cnt: Optional[int] = None,
             pivot_h_event: Optional[bool] = None, pivot_l_event: Optional[bool] = None,
             regime_label: Optional[str] = None) -> list[TradeRecord]:
        """Process a closed 5m bar. Advances bars_held and checks SL/TP1/trail/max_hold.

        Also pushes this bar's (high, low) into the rolling structural window
        used for runner SL management after TP1 hits. Only ``tick`` updates the
        window — intrabar_check sees the same window without advancing it.

        ``up_struct`` / ``dn_struct`` = current ICT state. If a trade's direction
        EXPLICITLY disagrees with the structure (long + dn_struct=True, short +
        up_struct=True), the whole stack is force-closed at ``close`` with
        reason ``choch_exit``. Unknown / neutral state does not trigger close.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return []
            pos.bars_held = self._compute_bars_held(pos)
            pos.recent_lows.append(low)
            pos.recent_highs.append(high)
            if len(pos.recent_lows) > STRUCTURAL_LOOKBACK_BARS:
                pos.recent_lows = pos.recent_lows[-STRUCTURAL_LOOKBACK_BARS:]
                pos.recent_highs = pos.recent_highs[-STRUCTURAL_LOOKBACK_BARS:]
            trades = self._process_bar(pos, symbol, high, low, close,
                                        up_struct=up_struct, dn_struct=dn_struct,
                                        bos_pivot_h=bos_pivot_h, bos_pivot_l=bos_pivot_l,
                                        regime_up=regime_up, regime_dn=regime_dn,
                                        ens_up_cnt=ens_up_cnt, ens_dn_cnt=ens_dn_cnt,
                                        pivot_h_event=pivot_h_event, pivot_l_event=pivot_l_event,
                                        regime_label=regime_label)
            # Persist bars_held / trail / best_price / recent_highs-lows advances
            # so the dashboard (and a crash-restart) see fresh state even on
            # bars that don't exit. _process_bar already saves on exit.
            if symbol in self.positions:
                self._save_state()
            return trades

    def intrabar_check(self, symbol: str, price: float,
                       bos_pivot_h: Optional[float] = None, bos_pivot_l: Optional[float] = None,
                       regime_up: Optional[bool] = None, regime_dn: Optional[bool] = None,
                       ens_up_cnt: Optional[int] = None, ens_dn_cnt: Optional[int] = None,
                       pivot_h_event: Optional[bool] = None, pivot_l_event: Optional[bool] = None,
                       regime_label: Optional[str] = None) -> list[TradeRecord]:
        """Intra-bar SL/TP1/trail check against a live mark price.

        Also refreshes bars_held from wall-clock so the dashboard and max_hold
        logic stay current between 5m ticks. Safe to call as often as you like;
        a no-op when there's no open position for the symbol.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return []
            pos.bars_held = self._compute_bars_held(pos)
            return self._process_bar(pos, symbol, high=price, low=price, close=price,
                                     bos_pivot_h=bos_pivot_h, bos_pivot_l=bos_pivot_l,
                                     regime_up=regime_up, regime_dn=regime_dn,
                                     ens_up_cnt=ens_up_cnt, ens_dn_cnt=ens_dn_cnt,
                                     pivot_h_event=pivot_h_event, pivot_l_event=pivot_l_event,
                                     regime_label=regime_label)

    def _process_bar(self, pos: OpenPosition, symbol: str,
                     high: float, low: float, close: float,
                     up_struct: Optional[bool] = None,
                     dn_struct: Optional[bool] = None,
                     bos_pivot_h: Optional[float] = None,
                     bos_pivot_l: Optional[float] = None,
                     regime_up: Optional[bool] = None,
                     regime_dn: Optional[bool] = None,
                     ens_up_cnt: Optional[int] = None,
                     ens_dn_cnt: Optional[int] = None,
                     pivot_h_event: Optional[bool] = None,
                     pivot_l_event: Optional[bool] = None,
                     regime_label: Optional[str] = None) -> list[TradeRecord]:
        """Core SL/TP1/trail/max_hold logic. Caller must hold self._lock.

        If ``up_struct`` / ``dn_struct`` are provided and explicitly disagree
        with the trade direction, a full CHoCH-exit is performed at ``close``
        BEFORE any other logic — the trend is invalidated, close everything.
        """
        trades: list[TradeRecord] = []

        # CHoCH trend-invalidation check — close all layers immediately.
        # SKIPPED for test_exit: test_bounce is a fade strategy, by design it
        # enters against the current structure (short at pivot H during an
        # uptrend, long at pivot L during a downtrend). CHoCH would fire on
        # bar 1 and kill every trade. The 3% SL + opposite-pivot exit is the
        # risk framework for this strategy.
        choch_exit = False
        if pos.exit_type not in ("test_exit", "pullback_exit"):
            if pos.side == "long" and dn_struct is True:
                choch_exit = True
            elif pos.side == "short" and up_struct is True:
                choch_exit = True
        if choch_exit:
            ep = close
            pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * pos.size
            pnl -= pos.notional * self.config.risk.commission_pct
            pnl -= pos.entry_fee
            pos.entry_fee = 0.0
            self.balance += pnl
            runner_r = None
            if pos.initial_sl > 0:
                risk_dist = abs(pos.entry_price - pos.initial_sl)
                if risk_dist > 0:
                    if pos.side == "long":
                        runner_r = (ep - pos.entry_price) / risk_dist
                    else:
                        runner_r = (pos.entry_price - ep) / risk_dist
            mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
            t = TradeRecord(
                timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                exit_reason="choch_exit", held_bars=pos.bars_held,
                runner_r=runner_r, favorable_excursion_atr=mfe_atr,
                entry_price=pos.entry_price,
                initial_sl=(pos.initial_sl or None),
                r=_trade_r(pos, ep),
                adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
            )
            trades.append(t); self.trade_history.append(t)
            del self.positions[symbol]
            log.info(
                f"[PAPER] CHoCH EXIT {symbol} {pos.side} @ ${ep:.4f} — trend invalidated "
                f"(layer={pos.n_pyramid_adds}/{self.config.pyramid.max_adds}) "
                f"P&L=${pnl:+.2f} held={pos.bars_held}b | balance=${self.balance:,.2f}"
            )
            _note_trade_safe({
                "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                "pnl": pnl, "reason": "choch_exit", "held_bars": pos.bars_held,
                "runner_r": runner_r, "favorable_excursion_atr": mfe_atr,
                "tp1_hit": pos.tp1_hit, "n_pyramid_adds": pos.n_pyramid_adds,
                "timestamp": datetime.utcnow().isoformat(),
                "event": "exit", "strategy": "whale_swing",
            })
            self._save_state()
            return trades

        # BOS / regime_flip structural exit — kicks in for positions opened
        # with a non-standard exit_type. Fires before SL/TP logic.
        if pos.exit_type in ("bos_structural", "bos_hybrid"):
            opp = bos_pivot_l if pos.side == "long" else bos_pivot_h
            if opp is not None and (
                (pos.side == "long" and close < opp) or
                (pos.side == "short" and close > opp)
            ):
                ep = opp
                pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * pos.size
                pnl -= pos.notional * self.config.risk.commission_pct
                pnl -= pos.entry_fee; pos.entry_fee = 0.0
                self.balance += pnl
                mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                    exit_reason="bos_exit", held_bars=pos.bars_held,
                    favorable_excursion_atr=mfe_atr,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                del self.positions[symbol]
                log.info(
                    f"[PAPER] BOS EXIT {symbol} {pos.side} @ ${ep:.4f} — opposing-pivot break "
                    f"P&L=${pnl:+.2f} held={pos.bars_held}b | balance=${self.balance:,.2f}"
                )
                _note_trade_safe({
                    "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                    "pnl": pnl, "reason": "bos_exit", "held_bars": pos.bars_held,
                    "favorable_excursion_atr": mfe_atr,
                    "timestamp": datetime.utcnow().isoformat(),
                    "event": "exit", "strategy": "whale_swing",
                })
                self._save_state()
                return trades

        if pos.exit_type == "regime_flip":
            want = regime_up if pos.side == "long" else regime_dn
            if want is False:  # filter turned off our direction
                ep = close
                pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * pos.size
                pnl -= pos.notional * self.config.risk.commission_pct
                pnl -= pos.entry_fee; pos.entry_fee = 0.0
                self.balance += pnl
                mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                    exit_reason="regime_exit", held_bars=pos.bars_held,
                    favorable_excursion_atr=mfe_atr,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                del self.positions[symbol]
                log.info(
                    f"[PAPER] REGIME EXIT {symbol} {pos.side} @ ${ep:.4f} — filter flipped off "
                    f"P&L=${pnl:+.2f} held={pos.bars_held}b | balance=${self.balance:,.2f}"
                )
                _note_trade_safe({
                    "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                    "pnl": pnl, "reason": "regime_exit", "held_bars": pos.bars_held,
                    "favorable_excursion_atr": mfe_atr,
                    "timestamp": datetime.utcnow().isoformat(),
                    "event": "exit", "strategy": "whale_swing",
                })
                self._save_state()
                return trades

        if pos.exit_type == "ensemble_hybrid":
            # Exit when the 5-filter consensus for this trade's direction
            # drops below (K-1). Symmetric lenient-exit threshold — the same
            # logic the backtester uses. K is stored on the position itself
            # (carried over from the config at entry).
            cnt = ens_up_cnt if pos.side == "long" else ens_dn_cnt
            k_exit = max(getattr(pos, "ensemble_k", 4) - 1, 1)
            if cnt is not None and cnt < k_exit:
                ep = close
                pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * pos.size
                pnl -= pos.notional * self.config.risk.commission_pct
                pnl -= pos.entry_fee; pos.entry_fee = 0.0
                self.balance += pnl
                mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                    exit_reason="ensemble_exit", held_bars=pos.bars_held,
                    favorable_excursion_atr=mfe_atr,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                del self.positions[symbol]
                log.info(
                    f"[PAPER] ENSEMBLE EXIT {symbol} {pos.side} @ ${ep:.4f} — "
                    f"consensus dropped to {cnt}<{k_exit} "
                    f"P&L=${pnl:+.2f} held={pos.bars_held}b | balance=${self.balance:,.2f}"
                )
                _note_trade_safe({
                    "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                    "pnl": pnl, "reason": "ensemble_exit", "held_bars": pos.bars_held,
                    "favorable_excursion_atr": mfe_atr,
                    "timestamp": datetime.utcnow().isoformat(),
                    "event": "exit", "strategy": "whale_swing",
                })
                self._save_state()
                return trades

        # pullback_exit — companion to pullback_in_regime entry. Exit when:
        #   1. Next opposite-side VALIDATED pivot confirms
        #   2. OR regime flips hostile (long + trend_down, short + trend_up)
        #   SL is already set at entry as flat 3% — enforced by standard SL path.
        if pos.exit_type == "pullback_exit":
            exit_trigger = None
            if pos.side == "long" and pivot_h_event:
                exit_trigger = "pivot_h"
            elif pos.side == "short" and pivot_l_event:
                exit_trigger = "pivot_l"
            elif pos.side == "long" and regime_label == "trend_down":
                exit_trigger = "regime_flip_red"
            elif pos.side == "short" and regime_label == "trend_up":
                exit_trigger = "regime_flip_green"
            if exit_trigger is not None:
                ep = close
                pnl = ((ep - pos.entry_price) if pos.side == "long"
                       else (pos.entry_price - ep)) * pos.size
                pnl -= pos.notional * self.config.risk.commission_pct
                pnl -= pos.entry_fee; pos.entry_fee = 0.0
                self.balance += pnl
                mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                    exit_reason=f"pullback_exit_{exit_trigger}", held_bars=pos.bars_held,
                    favorable_excursion_atr=mfe_atr,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                del self.positions[symbol]
                log.info(
                    f"[PAPER] PULLBACK EXIT {symbol} {pos.side} @ ${ep:.4f} — {exit_trigger} "
                    f"P&L=${pnl:+.2f} held={pos.bars_held}b | balance=${self.balance:,.2f}"
                )
                _note_trade_safe({
                    "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                    "pnl": pnl, "reason": f"pullback_exit_{exit_trigger}",
                    "held_bars": pos.bars_held, "favorable_excursion_atr": mfe_atr,
                    "timestamp": datetime.utcnow().isoformat(),
                    "event": "exit", "strategy": "whale_swing",
                })
                self._save_state()
                return trades

        # test_exit — fires when a NEW opposite-side pivot has confirmed since
        # entry (meaning the swing cycle has completed on the other side).
        # Identified by comparing current pivot level to the snapshot taken
        # at entry. SL stays active (3% flat) — checked further below.
        if pos.exit_type == "test_exit":
            cur_opp = bos_pivot_h if pos.side == "long" else bos_pivot_l
            entry_opp = pos.entry_pivot_h if pos.side == "long" else pos.entry_pivot_l
            if (cur_opp is not None and entry_opp and
                    abs(cur_opp - entry_opp) > 1e-9):
                ep = close
                pnl = ((ep - pos.entry_price) if pos.side == "long"
                       else (pos.entry_price - ep)) * pos.size
                pnl -= pos.notional * self.config.risk.commission_pct
                pnl -= pos.entry_fee; pos.entry_fee = 0.0
                self.balance += pnl
                mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                    exit_reason="test_exit", held_bars=pos.bars_held,
                    favorable_excursion_atr=mfe_atr,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                del self.positions[symbol]
                log.info(
                    f"[PAPER] TEST EXIT {symbol} {pos.side} @ ${ep:.4f} — "
                    f"opposite pivot flipped ({entry_opp:.4f}→{cur_opp:.4f}) "
                    f"P&L=${pnl:+.2f} held={pos.bars_held}b | balance=${self.balance:,.2f}"
                )
                _note_trade_safe({
                    "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                    "pnl": pnl, "reason": "test_exit", "held_bars": pos.bars_held,
                    "favorable_excursion_atr": mfe_atr,
                    "timestamp": datetime.utcnow().isoformat(),
                    "event": "exit", "strategy": "whale_swing",
                })
                self._save_state()
                return trades

        # Track max favorable excursion in ATR multiples (for post-trade analysis)
        if pos.entry_atr > 0:
            if pos.side == "long":
                fav = (high - pos.entry_price) / pos.entry_atr
                adv = (pos.entry_price - low) / pos.entry_atr
            else:
                fav = (pos.entry_price - low) / pos.entry_atr
                adv = (high - pos.entry_price) / pos.entry_atr
            if fav > pos.max_favorable_atr:
                pos.max_favorable_atr = fav
            if adv > pos.max_adverse_atr:
                pos.max_adverse_atr = adv

        # Update trail
        if pos.trail_offset > 0:
            if pos.side == "long":
                if high > pos.best_price: pos.best_price = high
                if not pos.trail_active and high >= pos.entry_price + pos.trail_offset:
                    pos.trail_active = True
                    log.info(
                        f"[PAPER] TRAIL ARMED {symbol} long — price reached "
                        f"${pos.entry_price + pos.trail_offset:.4f} (entry + "
                        f"{pos.trail_offset:.4f}); SL will now follow best_price "
                        f"(${pos.best_price:.4f}) minus trail offset"
                    )
                if pos.trail_active:
                    new_sl = pos.best_price - pos.trail_offset
                    if new_sl > pos.sl:
                        old_sl = pos.sl
                        # Throttle: only log when SL moves by ≥10% of trail offset
                        # (prevents one log line per 15s tick during a grind move)
                        if (new_sl - old_sl) >= pos.trail_offset * 0.1:
                            log.info(
                                f"[PAPER] TRAIL TIGHTEN {symbol} long — SL "
                                f"${old_sl:.4f} → ${new_sl:.4f} (best ${pos.best_price:.4f})"
                            )
                        pos.sl = new_sl
            else:
                if low < pos.best_price or pos.best_price == 0: pos.best_price = low
                if not pos.trail_active and low <= pos.entry_price - pos.trail_offset:
                    pos.trail_active = True
                    log.info(
                        f"[PAPER] TRAIL ARMED {symbol} short — price reached "
                        f"${pos.entry_price - pos.trail_offset:.4f} (entry - "
                        f"{pos.trail_offset:.4f}); SL will now follow best_price "
                        f"(${pos.best_price:.4f}) plus trail offset"
                    )
                if pos.trail_active:
                    new_sl = pos.best_price + pos.trail_offset
                    if new_sl < pos.sl:
                        old_sl = pos.sl
                        if (old_sl - new_sl) >= pos.trail_offset * 0.1:
                            log.info(
                                f"[PAPER] TRAIL TIGHTEN {symbol} short — SL "
                                f"${old_sl:.4f} → ${new_sl:.4f} (best ${pos.best_price:.4f})"
                            )
                        pos.sl = new_sl

        # TP1 partial
        if not pos.tp1_hit and pos.tp1 is not None:
            tp1_triggered = (pos.side == "long" and high >= pos.tp1) or \
                            (pos.side == "short" and low <= pos.tp1)
            if tp1_triggered:
                pos.tp1_hit = True
                exit_pct = pos.tp1_pct
                exit_size = pos.size * exit_pct
                ep = pos.tp1
                pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * exit_size
                pnl -= pos.notional * exit_pct * self.config.risk.commission_pct
                # burn this partial's share of the entry fee
                entry_fee_portion = pos.entry_fee * exit_pct
                pnl -= entry_fee_portion
                pos.entry_fee -= entry_fee_portion
                self.balance += pnl
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=exit_size, price=ep, notional=pos.notional * exit_pct,
                    pnl=pnl, exit_reason="tp1_partial", held_bars=pos.bars_held,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    favorable_excursion_atr=(pos.max_favorable_atr if pos.entry_atr > 0 else None),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t)
                self.trade_history.append(t)
                pos.size *= (1 - exit_pct)
                pos.notional *= (1 - exit_pct)
                # Structural SL for the runner: move SL to the most recent
                # swing low (long) / swing high (short) over the rolling
                # window. Falls back to BE if the window is empty (no ticks
                # yet) or the structural level is worse than the initial SL.
                new_sl_src = "BE"
                new_sl = pos.entry_price
                if pos.side == "long" and pos.recent_lows:
                    struct = min(pos.recent_lows)
                    if struct >= pos.initial_sl:
                        new_sl = struct
                        new_sl_src = "struct_low"
                    else:
                        new_sl = pos.initial_sl
                        new_sl_src = "initial_sl"
                elif pos.side == "short" and pos.recent_highs:
                    struct = max(pos.recent_highs)
                    if struct <= pos.initial_sl:
                        new_sl = struct
                        new_sl_src = "struct_high"
                    else:
                        new_sl = pos.initial_sl
                        new_sl_src = "initial_sl"
                pos.sl = new_sl
                log.info(
                    f"[PAPER] TP1 {symbol} {pos.side} — closed {exit_pct*100:.0f}% @ ${ep:.4f} "
                    f"P&L=${pnl:+.2f} → SL→${pos.sl:.4f} ({new_sl_src})"
                )

        # TP2 partial — fires only if configured (tp2 != None) and TP1 already hit
        if pos.tp1_hit and not pos.tp2_hit and pos.tp2 is not None and pos.tp2_pct > 0:
            tp2_triggered = (pos.side == "long" and high >= pos.tp2) or \
                            (pos.side == "short" and low <= pos.tp2)
            if tp2_triggered:
                pos.tp2_hit = True
                # tp2_pct is the fraction of ORIGINAL position to close. Current
                # pos.size is already reduced by TP1, so convert to a fraction
                # of the remaining size.
                remaining_frac = 1.0 - pos.tp1_pct
                exit_frac_of_remaining = pos.tp2_pct / max(remaining_frac, 1e-9)
                exit_frac_of_remaining = min(exit_frac_of_remaining, 1.0)
                exit_size = pos.size * exit_frac_of_remaining
                ep = pos.tp2
                pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * exit_size
                pnl -= pos.notional * exit_frac_of_remaining * self.config.risk.commission_pct
                entry_fee_portion = pos.entry_fee * exit_frac_of_remaining
                pnl -= entry_fee_portion
                pos.entry_fee -= entry_fee_portion
                self.balance += pnl
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=exit_size, price=ep, notional=pos.notional * exit_frac_of_remaining,
                    pnl=pnl, exit_reason="tp2_partial", held_bars=pos.bars_held,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    favorable_excursion_atr=(pos.max_favorable_atr if pos.entry_atr > 0 else None),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                pos.size *= (1 - exit_frac_of_remaining)
                pos.notional *= (1 - exit_frac_of_remaining)
                log.info(f"[PAPER] TP2 {symbol} {pos.side} — closed {pos.tp2_pct*100:.0f}% "
                         f"@ ${ep:.4f} P&L=${pnl:+.2f}")

        # TP3 partial — same logic, rides after TP2
        if pos.tp2_hit and not pos.tp3_hit and pos.tp3 is not None and pos.tp3_pct > 0:
            tp3_triggered = (pos.side == "long" and high >= pos.tp3) or \
                            (pos.side == "short" and low <= pos.tp3)
            if tp3_triggered:
                pos.tp3_hit = True
                remaining_frac = 1.0 - pos.tp1_pct - pos.tp2_pct
                exit_frac_of_remaining = pos.tp3_pct / max(remaining_frac, 1e-9)
                exit_frac_of_remaining = min(exit_frac_of_remaining, 1.0)
                exit_size = pos.size * exit_frac_of_remaining
                ep = pos.tp3
                pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * exit_size
                pnl -= pos.notional * exit_frac_of_remaining * self.config.risk.commission_pct
                entry_fee_portion = pos.entry_fee * exit_frac_of_remaining
                pnl -= entry_fee_portion
                pos.entry_fee -= entry_fee_portion
                self.balance += pnl
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=exit_size, price=ep, notional=pos.notional * exit_frac_of_remaining,
                    pnl=pnl, exit_reason="tp3_partial", held_bars=pos.bars_held,
                    entry_price=pos.entry_price,
                    initial_sl=(pos.initial_sl or None),
                    r=_trade_r(pos, ep),
                    favorable_excursion_atr=(pos.max_favorable_atr if pos.entry_atr > 0 else None),
                    adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
                )
                trades.append(t); self.trade_history.append(t)
                pos.size *= (1 - exit_frac_of_remaining)
                pos.notional *= (1 - exit_frac_of_remaining)
                log.info(f"[PAPER] TP3 {symbol} {pos.side} — closed {pos.tp3_pct*100:.0f}% "
                         f"@ ${ep:.4f} P&L=${pnl:+.2f}")

        # Time-stop — cut stale trades that haven't moved. Runs BEFORE SL check
        # so a trade that's bleeding slowly without ever touching SL still gets
        # cut. Narrow gate (skip after TP1 and after any pyramid add) so winners
        # aren't truncated. See TimeStopConfig for rationale.
        ts_cfg = self.config.time_stop
        time_stop_hit = False
        if (
            ts_cfg.enabled
            and pos.bars_held >= ts_cfg.stale_bars
            and pos.max_favorable_atr < ts_cfg.min_mfe_atr
            and not (ts_cfg.skip_after_tp1 and pos.tp1_hit)
            and not (ts_cfg.skip_after_pyramid and pos.n_pyramid_adds > 0)
        ):
            time_stop_hit = True

        # SL / max_hold
        # Disable standard SL for BOS/regime-exit positions — those use the
        # structural / regime trigger above. SL is still set at entry as a
        # record but not enforced here.
        # Pure structural/regime exit types ignore SL (their exit trigger IS
        # the structural break or filter flip). ensemble_hybrid keeps its flat
        # SL active as gap-protection (consensus drops are slower than price
        # gaps — a flat SL catches the tail-risk move).
        if pos.exit_type in ("bos_structural", "bos_hybrid", "regime_flip"):
            sl_hit = False
        else:
            sl_hit = (pos.side == "long" and low <= pos.sl) or (pos.side == "short" and high >= pos.sl)
        max_hold_hit = pos.bars_held >= pos.max_hold_bars
        if sl_hit or max_hold_hit or time_stop_hit:
            ep = pos.sl if sl_hit else close
            # Exit-reason priority: trail > structural > breakeven > stop_loss > time_stop > max_hold
            if pos.trail_active and sl_hit:
                reason = "trail_stop"
            elif pos.tp1_hit and sl_hit and abs(pos.sl - pos.entry_price) < 1e-9:
                reason = "breakeven"
            elif pos.tp1_hit and sl_hit:
                # SL was moved structurally post-TP1 — classify by direction
                runner_profit = (ep >= pos.entry_price) if pos.side == "long" else (ep <= pos.entry_price)
                reason = "structural_stop" if runner_profit else "runner_stop"
            elif sl_hit:
                reason = "stop_loss"
            elif time_stop_hit:
                reason = "time_stop"
            else:
                reason = "max_hold"
            pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * pos.size
            pnl -= pos.notional * self.config.risk.commission_pct
            # burn any remaining entry fee into the final exit
            pnl -= pos.entry_fee
            pos.entry_fee = 0.0
            self.balance += pnl
            # Runner R-multiple (per-unit price-P&L / initial-risk distance)
            runner_r: Optional[float] = None
            if pos.initial_sl > 0:
                risk_dist = abs(pos.entry_price - pos.initial_sl)
                if risk_dist > 0:
                    if pos.side == "long":
                        runner_r = (ep - pos.entry_price) / risk_dist
                    else:
                        runner_r = (pos.entry_price - ep) / risk_dist
            mfe_atr = pos.max_favorable_atr if pos.entry_atr > 0 else None
            t = TradeRecord(
                timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                exit_reason=reason, held_bars=pos.bars_held,
                runner_r=runner_r, favorable_excursion_atr=mfe_atr,
                entry_price=pos.entry_price,
                initial_sl=(pos.initial_sl or None),
                r=_trade_r(pos, ep),
                adverse_excursion_atr=(pos.max_adverse_atr if pos.entry_atr > 0 else None),
            )
            trades.append(t)
            self.trade_history.append(t)
            del self.positions[symbol]
            r_txt = f"R={runner_r:+.2f}" if runner_r is not None else "R=n/a"
            mfe_txt = f"MFE={mfe_atr:.1f}ATR" if mfe_atr is not None else "MFE=n/a"
            log.info(
                f"[PAPER] EXIT {symbol} {pos.side} @ ${ep:.4f} reason={reason} "
                f"held={pos.bars_held}b P&L=${pnl:+.2f} {r_txt} {mfe_txt} | "
                f"balance=${self.balance:,.2f}"
            )
            _note_trade_safe({
                "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                "pnl": pnl, "reason": reason, "held_bars": pos.bars_held,
                "runner_r": runner_r, "favorable_excursion_atr": mfe_atr,
                "tp1_hit": pos.tp1_hit,
                "timestamp": datetime.utcnow().isoformat(),
                "event": "exit", "strategy": "whale_swing",
            })
        self._save_state()
        return trades

    def open_count(self) -> int:
        return len(self.positions)

    def get_balance(self) -> float:
        unrealized = 0.0
        return self.balance + unrealized
