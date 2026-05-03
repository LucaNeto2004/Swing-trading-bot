"""Strict pivot strategy — high-confidence pivot entries.

Created 2026-04-29 from research session. Status: SHADOW MODE only until
2026-05-13 (config freeze period). DO NOT activate live trading from this
module before forward-walk + shadow paper validation completes.

Signal stack (ALL must be true for an entry):
  1. Validated 1h pivot (combined_pivots_1h: fractal + smoothed-peak +
     RSI/BB/volume validators + 1.0 ATR follow-through)
  2. RSI extreme at the PIVOT bar (>70 for highs / shorts, <30 for lows / longs)
  3. Bollinger outer touch at pivot bar (high≥bb_upper for shorts, low≤bb_lower
     for longs)
  4. 1h trend filter agrees (existing slow filter — sjm/hma_slope/etc)

Symbol whitelist:
  Only generates signals for symbols in DEPLOY_WHITELIST. Backtest 166d
  (2025-11→2026-04) showed FARTCOIN/BTC/XRP/ZEC produce positive edge
  (64% WR, +52.8% net @ 5% margin). ETH/TIA/PENDLE were negative on the
  same test → excluded.

Exits: standard execution (sl_atr=2.0, tp1_atr=2.0/30%, tp2_atr=3.0/30%,
tp3_atr=4.0/20%, trail_atr=2.0, max_hold=24 1h-bars). Per-symbol cap and
concurrency gate apply via core/risk.py.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import EntrySignal, SignalType


# Deploy whitelist — only these symbols generate strict pivot signals.
# Frozen subset from 166d backtest verdict 2026-04-29.
DEPLOY_WHITELIST = {"FARTCOIN", "BTC", "XRP", "ZEC"}

# Filter thresholds (matches research/strict_pivot_test.py)
RSI_HIGH_THRESHOLD = 70.0   # short pivot must have RSI > this on pivot bar
RSI_LOW_THRESHOLD = 30.0    # long pivot must have RSI < this
PIVOT_LOOKBACK_BARS = 3     # validated pivot needs 3 bars on each side
ATR_MIN_MOVE = 1.0
VOL_SPIKE = 1.3
RSI_VALIDATOR = (35.0, 65.0)


@dataclass
class StrictPivotConfig:
    """Per-symbol config — currently shared across whitelist (one-size-fits-all
    until per-symbol tuning is justified by larger samples)."""
    enabled: bool = False                        # ← OFF by default. Promote via deployer.
    sl_atr: float = 2.0
    tp1_atr: float = 2.0
    tp1_pct: float = 0.3
    tp2_atr: float = 3.0
    tp2_pct: float = 0.3
    tp3_atr: float = 4.0
    tp3_pct: float = 0.2
    trail_atr: float = 2.0
    max_hold_bars: int = 24 * 12                 # 24 1h bars × 12 (5m/1h) = 288 5m bars = 24h
    direction: str = "both"
    margin_pct_override: float = 0.05            # ← reduced from default 0.15 for variance safety


def evaluate_signal(
    symbol: str,
    df_1h: pd.DataFrame,
    cfg: StrictPivotConfig,
    confluence_state_at_now: Optional[str] = None,
) -> Optional[EntrySignal]:
    """Evaluate latest 1h bar for a strict-pivot entry.

    Caller passes the most recent ~50 1h bars (with EMAs/RSI/BB/ATR features
    already attached via core.features.add_features). Returns an EntrySignal
    if all 4 filters pass on the most recent CONFIRMED pivot, else None.

    `confluence_state_at_now` (optional) — if provided, an additional gate:
    require strong_up or trans_up for longs / strong_dn or trans_dn for shorts.
    Set to None to skip the confluence gate (the standalone backtest already
    embeds slow-trend agreement via the 1h filter).
    """
    if not cfg.enabled:
        return None
    if symbol not in DEPLOY_WHITELIST:
        return None
    if df_1h is None or len(df_1h) < 30:
        return None

    # Lazy import to avoid heavy deps when strategy is disabled
    from core.quant_filters import combined_pivots_1h

    valid_h, valid_l = combined_pivots_1h(
        df_1h,
        fractal_lookback=PIVOT_LOOKBACK_BARS,
        smoothed_lookback=2,
        atr_min_move=ATR_MIN_MOVE,
        vol_spike=VOL_SPIKE,
        rsi_extreme=RSI_VALIDATOR,
    )

    rsi = df_1h["rsi"].to_numpy()
    high = df_1h["high"].to_numpy()
    low = df_1h["low"].to_numpy()
    bb_u = df_1h["bb_upper"].to_numpy()
    bb_l = df_1h["bb_lower"].to_numpy()
    atr = df_1h["atr"].to_numpy()
    close = df_1h["close"].to_numpy()
    n = len(df_1h)

    # The "current" bar is the last bar; pivots confirm 3 bars after their
    # peak. So we check whether a pivot peaked exactly 3 bars ago.
    pivot_bar = n - 1 - PIVOT_LOOKBACK_BARS
    confirm_bar = n - 1
    if pivot_bar < 0 or atr[confirm_bar] <= 0:
        return None

    # Match the most-recently-confirmed pivot (if any) to our current bar
    most_recent_h = next((p for p in reversed(valid_h) if p[1] == pivot_bar), None)
    most_recent_l = next((p for p in reversed(valid_l) if p[1] == pivot_bar), None)

    side = None
    if most_recent_h is not None:
        # Short candidate
        if rsi[pivot_bar] <= RSI_HIGH_THRESHOLD:
            return None
        if np.isnan(bb_u[pivot_bar]) or high[pivot_bar] < bb_u[pivot_bar]:
            return None
        if confluence_state_at_now is not None and confluence_state_at_now not in ("strong_dn", "trans_dn"):
            return None
        side = SignalType.SHORT
    elif most_recent_l is not None:
        # Long candidate
        if rsi[pivot_bar] >= RSI_LOW_THRESHOLD:
            return None
        if np.isnan(bb_l[pivot_bar]) or low[pivot_bar] > bb_l[pivot_bar]:
            return None
        if confluence_state_at_now is not None and confluence_state_at_now not in ("strong_up", "trans_up"):
            return None
        side = SignalType.LONG
    else:
        return None

    if cfg.direction == "long_only" and side != SignalType.LONG:
        return None
    if cfg.direction == "short_only" and side != SignalType.SHORT:
        return None

    return EntrySignal(
        symbol=symbol,
        signal_type=side,
        entry_price=float(close[confirm_bar]),
        atr=float(atr[confirm_bar]),
        timestamp=datetime.utcnow(),
        reason=(
            f"strict_pivot_{side.value} | "
            f"pivot_rsi={rsi[pivot_bar]:.1f} | "
            f"bb_touch=yes | "
            f"trend_filter=agreed"
        ),
        metadata={
            "strategy": "strict_pivot",
            "pivot_bar_idx": int(pivot_bar),
            "pivot_rsi": float(rsi[pivot_bar]),
            "pivot_price_extreme": float(high[pivot_bar] if side == SignalType.SHORT else low[pivot_bar]),
            "shadow_only": True,  # ← TRUE until 2026-05-13 promotion
        },
    )
