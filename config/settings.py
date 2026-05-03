"""
Swing Trading Bot — configuration.

Whale-informed swing strategy based on 58bro.eth + nervousdegen.eth patterns.
Per-symbol params are loaded from config/deployed/whale_<SYMBOL>.json at runtime.
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()


# Per-symbol trading-window restrictions. Symbols not listed trade 24/7.
# xyz:SILVER is an xyz-deployer commodity pair — real liquidity only during
# London + NY sessions on weekdays. Entries outside this window get skipped;
# existing positions continue to be managed (SL/TP1/max_hold) normally.
TRADING_HOURS = {
    "xyz:SILVER": {
        "weekdays_only": True,          # Mon-Fri only
        "start_utc_hour": 8,            # London open
        "end_utc_hour": 22,             # NY close
    },
    # xyz:CL (WTI crude) trades ~24/5 — weekday gate only. Matches the weekday
    # mask used in the 2026-04-21 OOS backtest (41d / 12 OOS trades / PF 5.04).
    "xyz:CL": {
        "weekdays_only": True,
        "start_utc_hour": 0,            # 24h weekday window
        "end_utc_hour": 24,
    },
}


def is_tradeable_now(symbol: str, now_utc: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return (tradeable, reason). Reason is empty string when tradeable."""
    rules = TRADING_HOURS.get(symbol)
    if rules is None:
        return True, ""
    now = now_utc or datetime.now(timezone.utc)
    if rules.get("weekdays_only") and now.weekday() >= 5:
        return False, f"weekend ({now.strftime('%a')}) outside {symbol} trading window"
    h = now.hour
    if h < rules["start_utc_hour"] or h >= rules["end_utc_hour"]:
        return False, (f"{h:02d}:xx UTC outside {symbol} window "
                       f"({rules['start_utc_hour']:02d}-{rules['end_utc_hour']:02d})")
    return True, ""


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
    # xyz:SILVER demoted 2026-04-20 after intense backtest: −$140 P&L, PF 0.28,
    # failed in both train + test halves. Session filter alone wasn't enough;
    # the entry setup doesn't fit this instrument. Keep config file on disk for
    # future re-election but stop rotating live.
    # "xyz:SILVER": Instrument("xyz:SILVER", "Silver", 0.01, 0.01, 25),
    "BTC":       Instrument("BTC", "Bitcoin", 1.0, 0.00001, 40),
    # ETH demoted 2026-04-20 — tested 18 parameter/entry-type variants, EVERY ONE
    # failed out-of-sample walk-forward. Full-sample P&L was +$20 but driven
    # entirely by the first-half slice. Recent ETH regime doesn't fit our
    # entry setup. Keep config file on disk for future re-election.
    # "ETH":       Instrument("ETH", "Ethereum", 0.1, 0.0001, 25),
    # HYPE 2026-04-29 — KEPT ACTIVE BY USER OVERRIDE despite framework verdict
    # of FAIL. Forward-walk: 0/5 windows pass, PF mean 0.78 [0.53, 1.08].
    # 660-config intensive grid found 0 configs passing strict gate v1. Live
    # +$208 cumulative argues for keeping; framework argues for pausing. User
    # call: keep. Re-audit 2026-05-13 — if framework prediction holds (live
    # turns negative), pause. If live keeps winning, recalibrate the gate.
    "HYPE":      Instrument("HYPE", "HyperLiquid", 0.001, 0.01, 10),
    "ZEC":       Instrument("ZEC", "Zcash", 0.01, 0.01, 10),
    "XRP":       Instrument("XRP", "XRP", 0.0001, 1.0, 20),
    # kPEPE DEMOTED 2026-04-22 — all ensemble + single-filter cells failed the
    # 0.85 P(win) gate; baseline current config lost money (OOS $-70). Keep
    # config file on disk for future re-election after regime changes.
    # "kPEPE":     Instrument("kPEPE", "Kilo Pepe", 0.0000001, 1.0, 10),
    # FARTCOIN disabled 2026-04-22 — research/full_filter_grid.py tested 12 combos
    # (6 filters × 2 4h states); ALL 12 were unprofitable on 41d (best was −$42 /
    # PF 0.97 / OOS-PF 2.36 but only 2/4 quartiles positive, still losing on net).
    # Worst: sjm/4h (current deployed) at −$242 / PF 0.70 / 1/4 quartiles. Symbol
    # has no working variant — structurally broken like LIT. Keep config on disk
    # for future re-election after entry_type or sizing redesign.
    "FARTCOIN":  Instrument("FARTCOIN", "Fartcoin", 0.00001, 1.0, 10),
    # LIT disabled 2026-04-21 — research/lit_verdict.py showed current deployed
    # config (long_only, hma_slope, no 4h) fails scorecard: 41d PF 0.81, P&L −$148,
    # 1/4 quartiles positive. Filter-accuracy scorecard showed LIT UP calls are
    # actively contrarian (36.6% 4h-fwd hit rate). A VALIDATED rework exists —
    # short_only + structure + 4h-required: PF 1.73, OOS PF 1.05, 4/4 quartiles
    # positive, $+190 — but needs human approval before deploy (CLAUDE.md rule).
    # Re-enable by uncommenting + writing the validated config to whale_LIT.json.
    # "LIT":       Instrument("LIT", "Litentry", 0.0001, 1.0, 5),
    "ENA":       Instrument("ENA", "Ethena", 0.0001, 1.0, 10),
    # SOL RETIRED 2026-04-26 — failed cohort OOS (IS PF 1.74 → OOS PF 0.51,
    # OOS −$114, ±20% sens broke at 0.8×). Config in _retired/. Re-elect via
    # research/whale_oos.py if regime shifts.
    # "SOL":       Instrument("SOL", "Solana", 0.01, 0.01, 20),
    # xyz:CL REMOVED 2026-04-22 — decision to drop commodities from the bot
    # entirely. Trades on xyz HIP-3 were low-volume/low-leverage (5x cap) and
    # added a different exit framework (bb_touch + standard SL/TP) that didn't
    # fit with the test_bounce / ensemble strategies elsewhere. Config moved
    # to _retired/.
    # "xyz:CL":    Instrument("xyz:CL", "WTI Crude Oil", 0.01, 0.01, 5),
    # 2026-04-22: expanded universe — 7 near-miss candidates from the ensemble
    # expansion test (P(win) 0.72-0.80 on 41d, OOS+). Leverages verified against
    # HL /info meta endpoint 2026-04-22. Sizes use HL's szDecimals.
    "ETH":       Instrument("ETH", "Ethereum", 0.0001, 0.01, 25),
    # ARB RETIRED 2026-04-28 — backtest OOS only +$23 (gate-marginal); live
    # -$47 confirmed weak edge. Intensive grid (660 configs) found no
    # passing alternative. Config in _retired/.
    # "ARB":       Instrument("ARB", "Arbitrum", 0.1, 0.001, 10),
    # LINK RETIRED 2026-04-26 — failed cohort OOS (IS PF 7.05 → OOS PF 0.62,
    # n=8 underpowered, OOS −$19). Classic IS-overfit. Config in _retired/.
    # "LINK":      Instrument("LINK", "Chainlink", 0.1, 0.001, 10),
    "PENDLE":    Instrument("PENDLE", "Pendle", 1.0, 0.001, 5),
    "TIA":       Instrument("TIA", "Celestia", 0.1, 0.001, 5),
    # OP RETIRED 2026-04-28 — OOS +$99 thin, 4/6 live stops both directions,
    # choch_exits whipsawing. 660-config intensive grid found no passing
    # alternative. Config in _retired/.
    # "OP":        Instrument("OP", "Optimism", 0.1, 0.001, 5),
    # INJ RETIRED 2026-04-26 — failed cohort OOS (IS PF 3.97 → OOS n=0,
    # elected config never triggers on recent data). Config in _retired/.
    # "INJ":       Instrument("INJ", "Injective", 0.1, 0.01, 5),
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
class PyramidConfig:
    """Rules for adding to winning positions on BOS continuation.

    Two profiles by paper/live flag. Paper is moderately strict so forward-walk
    is meaningful; live is tighter so the edge survives real execution cost."""
    enabled: bool = True
    max_adds: int = 2                          # layers beyond the initial = cap on adds
    add_margin_pct: float = 0.05               # each add is 1/3 of initial 0.15 margin
    min_mfe_atr: float = 2.0                   # paper: must be 2 ATR in favor to add
    min_hours_since_last_action: float = 2.0   # paper: 2h gate between adds
    require_tp1_hit: bool = True               # proven winner only
    portfolio_leverage_cap: float = 5.0        # total notional / equity cap
    drawdown_lock_pct: float = 5.0             # if account DD from peak exceeds this, no adds
    banned_symbols: tuple = ()                 # e.g. memes in live profile

    @classmethod
    def paper(cls) -> "PyramidConfig":
        return cls()

    @classmethod
    def live(cls) -> "PyramidConfig":
        """Live profile — quarter size, 3 ATR, 4h gap, memes banned."""
        return cls(
            enabled=True,
            max_adds=2,
            add_margin_pct=0.0375,
            min_mfe_atr=3.0,
            min_hours_since_last_action=4.0,
            require_tp1_hit=True,
            portfolio_leverage_cap=5.0,
            drawdown_lock_pct=5.0,
            banned_symbols=("kPEPE", "FARTCOIN", "ENA", "ZEC"),  # ZEC added 2026-04-19 — backtest showed pyramid costs $681 / drops PF 2.24→1.65 on ZEC's config
        )


@dataclass
class TimeStopConfig:
    """Force-exit rule for stale trades that haven't moved.

    Motivated by 2026-04-21 study of HL trader 0x1aa780bb…: 1.44 trades/day,
    median hold 7h, avg LOSS only $2,719 vs avg win $18k — he cuts losers
    fast before they turn into real drawdowns. Our bot currently only exits
    via SL hit, TP ladder, CHoCH, or max_hold (15d). This adds an earlier
    gate: if the trade hasn't produced any meaningful excursion in N bars
    and hasn't been validated by TP1/pyramid, close it at market.

    Conservative defaults — gate is narrow so well-behaved trades don't fire.
    """
    # 2026-04-29: REVERTED to disabled. Backtest (mfe_scratch_grid.py against
    # current 8-symbol universe) showed every variant of time-stop tested is
    # NEGATIVE vs baseline (no time-stop): aggregate −$251 to −$735 across
    # stale ∈ {4,6,8,12,16} × mfe ∈ {0.3,0.5,0.8}. The 28%-of-losers-have-low-MFE
    # autopsy finding is real on live (11 trades, $993) but doesn't survive
    # backtest validation on the broader universe. Per `feedback_no_napkin_sims`,
    # honoring the OOS-validated baseline over post-hoc live fitting.
    enabled: bool = False
    stale_bars: int = 48                 # original — kept as documentation, gate is OFF
    min_mfe_atr: float = 0.3             # original — kept as documentation, gate is OFF
    skip_after_tp1: bool = True
    skip_after_pyramid: bool = True


@dataclass
class RiskConfig:
    """Paper-mode posture (2026-04-17): 58bro-default concurrency (2) BUT the
    three account-limit halts are disabled so the bot keeps trading 24/7 even
    through drawdowns — the point is to gather data across losing periods."""
    max_concurrent_positions: int = 4        # total cap — any mix (2026-04-22)
    # Group sub-caps equal to total → sub-caps don't bite, only the total binds.
    # Revert to split (e.g. 4 crypto / 1 commodity) if we need to reserve a
    # commodity slot again.
    max_crypto_concurrent: int = 4
    max_commodity_concurrent: int = 4
    max_daily_loss_pct: float = 1.0          # disabled — let it run through −100%
    max_account_drawdown_pct: float = 1.0    # disabled — no peak-to-trough halt
    max_consecutive_losses: int = 9999       # disabled — no consec-loss halt
    # Per-symbol 24h loss cap — pause a symbol for 24h if its realized loss in
    # the trailing 24h exceeds this fraction of starting-balance-of-day. Narrower
    # dimension than the global halts (those stay disabled); this one isolates
    # one bleeding symbol without stopping the rest of the book. Designed after
    # 2026-04-21 ZEC event: 3 SLs in 6h = −$384 on a $10k account (−3.8%).
    per_symbol_daily_loss_pct: float = 0.02  # 2% of equity — pause symbol 24h
    per_symbol_cap_enabled: bool = True
    # No trading-hours restriction — whale strategies hold multi-day, 24/7 ok
    # 2026-04-20: bumped 0.00006 → 0.00030 to reflect HL tier-0 crypto-perp
    # reality. Old value (0.006%/side) was a high-tier institutional blend.
    # New value (0.030%/side) = realistic 50/50 maker/taker on tier-0 crypto
    # perps (maker 0.015%, taker 0.045%). Bot now trades 100% native crypto
    # perps (SILVER+ETH demoted), so HIP-3 commodity discount doesn't apply.
    commission_pct: float = 0.00030          # HL tier 0 crypto perp, 50/50 mix


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
    # Per user's 2026-04-19 call: run live-profile pyramid rules even in paper
    # so forward-walk is realistic for the eventual live flip.
    pyramid: PyramidConfig = field(default_factory=PyramidConfig.live)
    time_stop: TimeStopConfig = field(default_factory=TimeStopConfig)

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
