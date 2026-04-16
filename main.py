"""
Swing Trading Bot — main loop.

Strategy: whale swing (58bro + nervousdegen informed).
Pipeline: fetch candles → features → strategy per symbol → risk gate → execution.

Paper trading by default. Live trading requires explicit unlock (paper_trading=False).
Runs all symbols in config.instruments — max_concurrent_positions caps concurrency.
"""
import os
import signal as sig
import sys
import time
from datetime import datetime

from config.settings import load_config
from config.deployer import load_all
from core.alerts import AlertManager
from core.data import DataManager
from core.execution import PaperTrader
from core.risk import RiskGate
from strategies.base import SignalType
from strategies.whale_swing import WhaleSwingConfig, WhaleSwingStrategy
from utils.logger import setup_logger

log = setup_logger("main")

print("\033]0;swing-bot\007", end="", flush=True)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(_BASE_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, "data"), exist_ok=True)


class SwingBot:
    def __init__(self):
        self.config = load_config()
        self.running = False
        self.data = DataManager(self.config)
        self.risk = RiskGate(self.config)
        self.paper = PaperTrader(self.config)
        self.alerts = AlertManager(self.config)

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
        log.info("=" * 60)
        self.alerts.send_status("online", f"Swing bot {mode} on {net} — {len(self.strategies)} symbols")

        cycle = 0
        while self.running:
            try:
                cycle += 1
                log.info(f"--- Cycle {cycle} | {datetime.now().strftime('%H:%M:%S')} ---")
                self._run_cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)
                self.alerts.send_status("error", str(e))
            if self.running:
                self._wait_for_next_5m_candle()
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
        # Update portfolio state
        self.risk.update_portfolio(self.paper.get_balance(), self.paper.open_count())
        if self.risk.kill_switch or self.risk.account_dd_halt or self.risk.consecutive_loss_halt:
            log.warning("Risk halt active — skipping signals")
            return

        for sym, strategy in self.strategies.items():
            try:
                is_new = self.data.refresh(sym)
                df5 = self.data.df_5m.get(sym)
                if df5 is None or df5.empty or len(df5) < 55:
                    continue

                # Always tick the position — SL/TP1/trail check every cycle
                latest = df5.iloc[-1]
                trades = self.paper.tick(
                    sym,
                    high=float(latest['high']),
                    low=float(latest['low']),
                    close=float(latest['close']),
                )
                for t in trades:
                    self.risk.record_trade(t.pnl or 0.0)
                    self.alerts.send_exit(t.symbol, t.side, t.price,
                                          t.pnl or 0.0, t.exit_reason, t.held_bars)

                # Only evaluate NEW entry signals on new 5m bar close
                if not is_new:
                    continue

                # Skip if already in a position for this symbol
                if sym in self.paper.positions:
                    continue

                up_1h = bool(self.data.up_1h[sym][-1]) if sym in self.data.up_1h else True
                dn_1h = bool(self.data.dn_1h[sym][-1]) if sym in self.data.dn_1h else True
                signal = strategy.evaluate(sym, df5, up_1h, dn_1h)
                if signal is None:
                    continue

                passed, reason = self.risk.check(signal)
                if not passed:
                    log.info(f"REJECTED {sym} {signal.signal_type.value}: {reason}")
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

            except Exception as e:
                log.error(f"{sym}: cycle error — {e}", exc_info=True)

    def _shutdown(self, signum=None, frame=None):
        log.info("Shutdown signal received")
        self.running = False

    def _on_shutdown(self):
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
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
