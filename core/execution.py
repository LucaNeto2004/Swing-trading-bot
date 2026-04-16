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
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from config.settings import BotConfig
from strategies.base import EntrySignal, SignalType
from strategies.whale_swing import WhaleSwingConfig
from utils.logger import setup_logger

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
    price: float            # fill price
    notional: float         # size × price
    pnl: Optional[float] = None
    exit_reason: str = ""   # "stop_loss" | "tp1_partial" | "trail_stop" | "max_hold"
    held_bars: int = 0


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
    max_hold_bars: int = 288
    bars_held: int = 0
    cfg_label: str = ""          # config label for reporting
    strategy_name: str = "whale_swing"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OpenPosition":
        return cls(**d)


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
        except Exception as e:
            log.warning(f"Failed to load paper state: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(PAPER_STATE_FILE), exist_ok=True)
            state = {
                "balance": self.balance,
                "starting_balance": self.starting_balance,
                "positions": {sym: p.to_dict() for sym, p in self.positions.items()},
                "saved_at": datetime.now().isoformat(),
            }
            tmp = PAPER_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, PAPER_STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save paper state: {e}")

    def open(self, signal: EntrySignal, cfg: WhaleSwingConfig, cfg_label: str) -> Optional[OpenPosition]:
        if signal.symbol in self.positions:
            log.info(f"{signal.symbol}: already has open position, skipping entry")
            return None
        side = "long" if signal.signal_type == SignalType.LONG else "short"
        sizing = self.config.sizing
        margin = self.balance * sizing.margin_pct
        notional = margin * sizing.set_leverage
        price = signal.entry_price
        size = notional / price
        atr = signal.atr
        sl = price - atr * cfg.sl_atr if side == "long" else price + atr * cfg.sl_atr
        tp1 = None
        if cfg.tp1_atr > 0:
            tp1 = price + atr * cfg.tp1_atr if side == "long" else price - atr * cfg.tp1_atr
        trail_offset = atr * cfg.trail_atr if cfg.trail_atr > 0 else 0.0
        pos = OpenPosition(
            symbol=signal.symbol, side=side, entry_price=price, size=size, notional=notional,
            entry_atr=atr, entry_bar_ts=str(signal.timestamp),
            sl=sl, tp1=tp1, tp1_pct=cfg.tp1_pct, trail_offset=trail_offset,
            best_price=price, max_hold_bars=cfg.max_hold_bars, cfg_label=cfg_label,
        )
        self.positions[signal.symbol] = pos
        # Commission on entry
        self.balance -= notional * self.config.risk.commission_pct
        self._save_state()
        log.info(
            f"[PAPER] ENTRY {signal.symbol} {side.upper()} @ ${price:.4f} "
            f"size={size:.4f} notional=${notional:,.2f} SL=${sl:.4f} "
            f"{'TP1=$%.4f' % tp1 if tp1 else 'noTP1'}"
        )
        _note_trade_safe({
            "symbol": signal.symbol, "side": side, "price": price, "size": size,
            "notional": notional, "sl": sl, "tp1": tp1, "reason": signal.reason,
            "timestamp": str(signal.timestamp), "event": "entry",
            "strategy": "whale_swing", "cfg_label": cfg_label,
        })
        return pos

    def tick(self, symbol: str, high: float, low: float, close: float) -> list[TradeRecord]:
        """Process one bar for this symbol. Returns any trades (partials, exits) fired."""
        pos = self.positions.get(symbol)
        if pos is None:
            return []
        pos.bars_held += 1
        trades: list[TradeRecord] = []

        # Update trail
        if pos.trail_offset > 0:
            if pos.side == "long":
                if high > pos.best_price: pos.best_price = high
                if not pos.trail_active and high >= pos.entry_price + pos.trail_offset:
                    pos.trail_active = True
                if pos.trail_active:
                    new_sl = pos.best_price - pos.trail_offset
                    if new_sl > pos.sl: pos.sl = new_sl
            else:
                if low < pos.best_price or pos.best_price == 0: pos.best_price = low
                if not pos.trail_active and low <= pos.entry_price - pos.trail_offset:
                    pos.trail_active = True
                if pos.trail_active:
                    new_sl = pos.best_price + pos.trail_offset
                    if new_sl < pos.sl: pos.sl = new_sl

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
                self.balance += pnl
                t = TradeRecord(
                    timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                    size=exit_size, price=ep, notional=pos.notional * exit_pct,
                    pnl=pnl, exit_reason="tp1_partial", held_bars=pos.bars_held,
                )
                trades.append(t)
                self.trade_history.append(t)
                pos.size *= (1 - exit_pct)
                pos.notional *= (1 - exit_pct)
                pos.sl = pos.entry_price  # breakeven
                log.info(
                    f"[PAPER] TP1 {symbol} {pos.side} — closed {exit_pct*100:.0f}% @ ${ep:.4f} "
                    f"P&L=${pnl:+.2f} → SL moved to BE (${pos.sl:.4f})"
                )

        # SL / max_hold
        sl_hit = (pos.side == "long" and low <= pos.sl) or (pos.side == "short" and high >= pos.sl)
        max_hold_hit = pos.bars_held >= pos.max_hold_bars
        if sl_hit or max_hold_hit:
            ep = pos.sl if sl_hit else close
            reason = ("trail_stop" if pos.trail_active and sl_hit else
                      "breakeven" if pos.tp1_hit and sl_hit and abs(pos.sl - pos.entry_price) < 1e-9 else
                      "stop_loss" if sl_hit else "max_hold")
            pnl = ((ep - pos.entry_price) if pos.side == "long" else (pos.entry_price - ep)) * pos.size
            pnl -= pos.notional * self.config.risk.commission_pct
            self.balance += pnl
            t = TradeRecord(
                timestamp=datetime.utcnow(), symbol=symbol, side=pos.side,
                size=pos.size, price=ep, notional=pos.notional, pnl=pnl,
                exit_reason=reason, held_bars=pos.bars_held,
            )
            trades.append(t)
            self.trade_history.append(t)
            del self.positions[symbol]
            log.info(
                f"[PAPER] EXIT {symbol} {pos.side} @ ${ep:.4f} reason={reason} "
                f"held={pos.bars_held}b P&L=${pnl:+.2f} | balance=${self.balance:,.2f}"
            )
            _note_trade_safe({
                "symbol": symbol, "side": pos.side, "price": ep, "size": pos.size,
                "pnl": pnl, "reason": reason, "held_bars": pos.bars_held,
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
