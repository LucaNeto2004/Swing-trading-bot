"""
Swing Trading Bot — configuration.

Whale-informed swing strategy based on 58bro.eth + nervousdegen.eth patterns.
Per-symbol params are loaded from config/deployed/whale_<SYMBOL>.json at runtime.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Instrument:
    symbol: str
    name: str
    tick_size: float
    lot_size: float
    hl_max_leverage: int  # highest set leverage HL allows on this pair


# All 11 symbols from the 2026-04-16 backtest. Paper mode runs all of them as a
# broad watchlist; the risk gate's max_concurrent_positions cap (2) enforces
# 58bro-style concurrency discipline. Promote/demote based on forward-walk.
# tick/lot_size are placeholders — only relevant when live orders ship.
INSTRUMENTS = {
    "xyz:SILVER": Instrument("xyz:SILVER", "Silver", 0.01, 0.01, 5),
    "BTC":       Instrument("BTC", "Bitcoin", 1.0, 0.00001, 40),
    "ETH":       Instrument("ETH", "Ethereum", 0.1, 0.0001, 25),
    "HYPE":      Instrument("HYPE", "HyperLiquid", 0.001, 0.01, 10),
    "ZEC":       Instrument("ZEC", "Zcash", 0.01, 0.01, 20),
    "XRP":       Instrument("XRP", "XRP", 0.0001, 1.0, 20),
    "kPEPE":     Instrument("kPEPE", "Kilo Pepe", 0.0000001, 1.0, 10),
    "FARTCOIN":  Instrument("FARTCOIN", "Fartcoin", 0.00001, 1.0, 10),
    "BIO":       Instrument("BIO", "Bio Protocol", 0.00001, 1.0, 10),
    "ORDI":      Instrument("ORDI", "Ordinals", 0.001, 0.01, 10),
    "LIT":       Instrument("LIT", "Litentry", 0.0001, 1.0, 10),
}


@dataclass
class WhaleSizingConfig:
    """58bro model — high set leverage × low margin % = capital-efficient risk."""
    account_size: float = 10_000.0           # paper starting balance
    margin_pct: float = 0.15                 # 15% of equity as margin per position
    set_leverage: int = 40                   # high set lev for capital efficiency
    # Effective leverage = margin_pct × set_leverage = 0.15 × 40 = 6.0x

    @property
    def effective_leverage(self) -> float:
        return self.margin_pct * self.set_leverage

    @property
    def liquidation_distance_pct(self) -> float:
        return 100.0 / self.effective_leverage


@dataclass
class RiskConfig:
    """Tight whale risk gate — 58bro-style capital preservation."""
    max_concurrent_positions: int = 2        # match 58bro's 1-2 concurrent shorts
    max_daily_loss_pct: float = 0.05         # 5% daily loss → kill switch
    max_account_drawdown_pct: float = 0.15   # 15% from peak → halt everything
    max_consecutive_losses: int = 5          # halt after N losing trades in a row
    # No trading-hours restriction — whale strategies hold multi-day, 24/7 ok
    commission_pct: float = 0.00006          # HL blended maker/taker


@dataclass
class BotConfig:
    # HyperLiquid
    private_key: str = field(default_factory=lambda: os.getenv("HL_PRIVATE_KEY", ""))
    account_address: str = field(default_factory=lambda: os.getenv("HL_ACCOUNT_ADDRESS", ""))
    testnet: bool = field(default_factory=lambda: os.getenv("HL_TESTNET", "true").lower() == "true")

    # Strategy
    instruments: dict = field(default_factory=lambda: INSTRUMENTS)
    sizing: WhaleSizingConfig = field(default_factory=WhaleSizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Loop
    loop_interval_seconds: int = 30
    entry_tf: str = "5m"
    trend_tf: str = "1h"
    lookback_5m: int = 500       # ~40 hours — enough for features + a few entries
    lookback_1h: int = 200       # ~8 days — enough to fire 1h trend lookup

    # Mode
    paper_trading: bool = True

    # Discord
    discord_webhook_trades: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_TRADES", ""))
    discord_webhook_alerts: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_ALERTS", ""))
    discord_webhook_reports: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_REPORTS", ""))

    # Obsidian
    vault_path: str = field(default_factory=lambda: os.getenv(
        "VAULT_PATH", "/Users/lucaneto/obsidian vault/Trading"
    ))

    # Logging
    log_level: str = "INFO"


def load_config() -> BotConfig:
    return BotConfig()
