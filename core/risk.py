"""
Risk Gate — deterministic hard rules. No AI. Every entry signal passes through
here. Any fail → signal rejected. Risk state persists across restarts.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from config.settings import BotConfig
from strategies.base import EntrySignal
from utils.logger import setup_logger

log = setup_logger("risk")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISK_STATE_FILE = os.path.join(_BASE_DIR, "data", "risk_state.json")


@dataclass
class PortfolioState:
    account_balance: float = 0.0
    open_positions: int = 0
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
                "saved_at": datetime.now().isoformat(),
            }
            tmp = RISK_STATE_FILE + ".tmp"
            os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, RISK_STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save risk state: {e}")

    def update_portfolio(self, balance: float, open_positions: int):
        today = date.today()
        if self.portfolio.daily_date != today:
            self.portfolio.daily_date = today
            self.portfolio.daily_pnl = 0.0
            self.portfolio.starting_balance = balance
            self.kill_switch = False
        if self.portfolio.starting_balance == 0.0:
            self.portfolio.starting_balance = balance
        self.portfolio.account_balance = balance
        self.portfolio.open_positions = open_positions
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

    def check(self, signal: EntrySignal) -> "tuple[bool, str]":
        if self.kill_switch:
            return False, "KILL SWITCH — daily loss limit breached"
        if self.account_dd_halt:
            return False, "ACCOUNT DD HALT — 15% peak-to-trough breach"
        if self.consecutive_loss_halt:
            return False, f"CONSECUTIVE LOSS HALT — {self.consecutive_losses} losses in a row"
        if self.portfolio.open_positions >= self.rules.max_concurrent_positions:
            return False, f"max concurrent positions ({self.rules.max_concurrent_positions}) reached"
        if self.portfolio.starting_balance > 0:
            daily_pct = self.portfolio.daily_pnl / self.portfolio.starting_balance
            if daily_pct <= -self.rules.max_daily_loss_pct:
                self.kill_switch = True
                return False, f"daily loss {daily_pct*100:.2f}% — kill switch engaged"
        return True, "passed"

    def record_trade(self, pnl: float):
        self.portfolio.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.rules.max_consecutive_losses:
                self.consecutive_loss_halt = True
                log.error(f"CONSECUTIVE LOSS HALT — {self.consecutive_losses} losses")
        else:
            self.consecutive_losses = 0
        self.save_state()
