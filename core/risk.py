"""
Risk Gate — deterministic hard rules. No AI. Every entry signal passes through
here. Any fail → signal rejected. Risk state persists across restarts.
"""
import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from config.settings import BotConfig
from strategies.base import EntrySignal
from utils.logger import setup_logger

log = setup_logger("risk")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISK_STATE_FILE = os.path.join(_BASE_DIR, "data", "risk_state.json")


def symbol_group(symbol: str) -> str:
    """xyz: prefix marks HL HIP-3 commodity perps; everything else is crypto."""
    return "commodity" if symbol.startswith("xyz:") else "crypto"


@dataclass
class PortfolioState:
    account_balance: float = 0.0
    open_positions: int = 0
    open_by_group: dict = field(default_factory=lambda: {"crypto": 0, "commodity": 0})
    daily_pnl: float = 0.0
    daily_date: date = field(default_factory=date.today)
    starting_balance: float = 0.0


class RiskGate:
    def __init__(self, config: BotConfig):
        self.config = config
        self.rules = config.risk
        self.portfolio = PortfolioState()
        self.kill_switch: bool = False
        self.consecutive_losses: int = 0
        self.consecutive_loss_halt: bool = False
        self._account_peak_balance: float = 0.0
        self.account_dd_halt: bool = False
        # Per-symbol 24h rolling loss tracker: symbol -> list of (ts_iso, pnl).
        # Entries older than 24h are pruned on read. When total loss in window
        # exceeds per_symbol_daily_loss_pct × current equity, the symbol is
        # paused for 24h from the last loss.
        self._symbol_pnl_window: dict[str, list[tuple[str, float]]] = {}
        self._load_state()

    def _load_state(self):
        if not os.path.exists(RISK_STATE_FILE):
            return
        try:
            with open(RISK_STATE_FILE) as f:
                s = json.load(f)
            self.portfolio.daily_pnl = s.get("daily_pnl", 0.0)
            self.portfolio.daily_date = date.fromisoformat(s.get("daily_date", date.today().isoformat()))
            self.portfolio.starting_balance = s.get("starting_balance", 0.0)
            self.kill_switch = s.get("kill_switch", False)
            self.consecutive_losses = s.get("consecutive_losses", 0)
            self.consecutive_loss_halt = s.get("consecutive_loss_halt", False)
            self._account_peak_balance = s.get("account_peak_balance", 0.0)
            self.account_dd_halt = s.get("account_dd_halt", False)
            raw = s.get("symbol_pnl_window", {}) or {}
            self._symbol_pnl_window = {
                sym: [(ts, float(p)) for ts, p in entries]
                for sym, entries in raw.items()
            }
        except Exception as e:
            log.warning(f"Failed to load risk state: {e}")

    def save_state(self):
        try:
            state = {
                "daily_pnl": self.portfolio.daily_pnl,
                "daily_date": self.portfolio.daily_date.isoformat(),
                "starting_balance": self.portfolio.starting_balance,
                "kill_switch": self.kill_switch,
                "consecutive_losses": self.consecutive_losses,
                "consecutive_loss_halt": self.consecutive_loss_halt,
                "account_peak_balance": self._account_peak_balance,
                "account_dd_halt": self.account_dd_halt,
                "symbol_pnl_window": self._symbol_pnl_window,
                "saved_at": datetime.now().isoformat(),
            }
            tmp = RISK_STATE_FILE + ".tmp"
            os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, RISK_STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save risk state: {e}")

    def update_portfolio(self, balance: float, open_symbols):
        """open_symbols: iterable of open-position symbols. Back-compat: an int
        still works (treated as crypto-only total) so older callers don't break."""
        today = date.today()
        if self.portfolio.daily_date != today:
            self.portfolio.daily_date = today
            self.portfolio.daily_pnl = 0.0
            self.portfolio.starting_balance = balance
            self.kill_switch = False
        if self.portfolio.starting_balance == 0.0:
            self.portfolio.starting_balance = balance
        self.portfolio.account_balance = balance
        if isinstance(open_symbols, int):
            self.portfolio.open_positions = open_symbols
            self.portfolio.open_by_group = {"crypto": open_symbols, "commodity": 0}
        else:
            syms = list(open_symbols)
            self.portfolio.open_positions = len(syms)
            self.portfolio.open_by_group = {"crypto": 0, "commodity": 0}
            for s in syms:
                self.portfolio.open_by_group[symbol_group(s)] += 1
        if balance > self._account_peak_balance:
            self._account_peak_balance = balance
        if self._account_peak_balance > 0:
            dd = (self._account_peak_balance - balance) / self._account_peak_balance
            if dd >= self.rules.max_account_drawdown_pct:
                self.account_dd_halt = True
                log.error(
                    f"ACCOUNT DD HALT — {dd*100:.1f}% drawdown from peak "
                    f"${self._account_peak_balance:,.2f} → ${balance:,.2f}"
                )

    def _prune_symbol_window(self, symbol: str) -> float:
        """Drop entries older than 24h and return the 24h loss sum (>= 0 means gain)."""
        entries = self._symbol_pnl_window.get(symbol, [])
        cutoff = datetime.now() - timedelta(hours=24)
        fresh = [(ts, p) for ts, p in entries
                 if datetime.fromisoformat(ts) >= cutoff]
        if len(fresh) != len(entries):
            self._symbol_pnl_window[symbol] = fresh
        return sum(p for _, p in fresh)

    def symbol_loss_status(self, symbol: str) -> "tuple[bool, float, float]":
        """Return (halted, loss_pct_of_equity, loss_cap_pct). halted=True when
        the 24h realized loss on this symbol exceeds the per-symbol cap."""
        if not getattr(self.rules, "per_symbol_cap_enabled", False):
            return False, 0.0, 0.0
        cap_pct = getattr(self.rules, "per_symbol_daily_loss_pct", 0.02)
        equity = self.portfolio.account_balance or self.portfolio.starting_balance or 0.0
        if equity <= 0:
            return False, 0.0, cap_pct
        pnl_24h = self._prune_symbol_window(symbol)
        loss_pct = -pnl_24h / equity if pnl_24h < 0 else 0.0
        return loss_pct >= cap_pct, loss_pct, cap_pct

    def check(self, signal: EntrySignal) -> "tuple[bool, str]":
        if self.kill_switch:
            return False, "KILL SWITCH — daily loss limit breached"
        if self.account_dd_halt:
            return False, "ACCOUNT DD HALT — 15% peak-to-trough breach"
        if self.consecutive_loss_halt:
            return False, f"CONSECUTIVE LOSS HALT — {self.consecutive_losses} losses in a row"
        if self.portfolio.open_positions >= self.rules.max_concurrent_positions:
            return False, f"max concurrent positions ({self.rules.max_concurrent_positions}) reached"
        group = symbol_group(signal.symbol)
        group_cap = (self.rules.max_commodity_concurrent if group == "commodity"
                     else self.rules.max_crypto_concurrent)
        group_open = self.portfolio.open_by_group.get(group, 0)
        if group_open >= group_cap:
            return False, f"max {group} concurrent ({group_cap}) reached — slot reserved for other group"
        halted, loss_pct, cap_pct = self.symbol_loss_status(signal.symbol)
        if halted:
            return False, (f"{signal.symbol}: 24h loss {loss_pct*100:.2f}% "
                           f"≥ cap {cap_pct*100:.2f}% — symbol paused")
        if self.portfolio.starting_balance > 0:
            daily_pct = self.portfolio.daily_pnl / self.portfolio.starting_balance
            if daily_pct <= -self.rules.max_daily_loss_pct:
                self.kill_switch = True
                return False, f"daily loss {daily_pct*100:.2f}% — kill switch engaged"
        return True, "passed"

    def record_trade(self, pnl: float, symbol: Optional[str] = None):
        self.portfolio.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.rules.max_consecutive_losses:
                self.consecutive_loss_halt = True
                log.error(f"CONSECUTIVE LOSS HALT — {self.consecutive_losses} losses")
        else:
            self.consecutive_losses = 0
        if symbol:
            self._symbol_pnl_window.setdefault(symbol, []).append(
                (datetime.now().isoformat(), float(pnl))
            )
            halted, loss_pct, cap_pct = self.symbol_loss_status(symbol)
            if halted:
                log.warning(
                    f"SYMBOL PAUSE {symbol} — 24h loss {loss_pct*100:.2f}% "
                    f"≥ cap {cap_pct*100:.2f}% — no new entries for 24h"
                )
        self.save_state()
