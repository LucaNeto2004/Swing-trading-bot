"""
Adaptive SL/TP builder — shared between commodities-bot and crypto-bot.

Computes per-symbol stop profile from rolling 90-day returns:
  - long_sl_mult / short_sl_mult (asymmetric, skew-adjusted ATR multipliers)
  - target_rr (regime-aware R:R target)
  - trail_mult (trail offset once armed)
  - trail_arm_atr (how many ATRs in profit before trail arms)
  - pause flag (set when distribution shifts or vol regime changes)

Writes a JSON file the bot's execution layer reads at entry time.
Pure numpy/pandas — no scipy/statsmodels dependency so it runs in either bot's venv.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Callable, Dict, Any

import numpy as np
import pandas as pd


DEFAULT_BASE_SL = 0.7
DEFAULT_TARGET_RR = 2.0
DEFAULT_TRAIL_MULT = 0.6
DEFAULT_TRAIL_ARM_ATR = 1.0

RECENT_N = 50        # bars used for "recent" distribution vs "historical"
MIN_BARS = 120       # refuse to compute on short history


def _safe_float(x, default=0.0) -> float:
    try:
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except Exception:
        return default


def compute_profile(symbol: str, candles: pd.DataFrame,
                    base_sl: float = DEFAULT_BASE_SL) -> Dict[str, Any]:
    """Compute adaptive stop profile for one symbol from a candle DataFrame.

    candles must have a 'close' column. Uses log returns for stability on
    high-volatility assets (HYPE in particular).
    """
    if candles is None or len(candles) < MIN_BARS:
        return {
            "long_sl_mult": base_sl,
            "short_sl_mult": base_sl,
            "target_rr": DEFAULT_TARGET_RR,
            "trail_mult": DEFAULT_TRAIL_MULT,
            "trail_arm_atr": DEFAULT_TRAIL_ARM_ATR,
            "pause": False,
            "reason": f"insufficient history ({0 if candles is None else len(candles)} bars) — using defaults",
            "stats": {},
        }

    closes = candles["close"].astype(float)
    returns = np.log(closes / closes.shift(1)).dropna()
    if len(returns) < MIN_BARS:
        return {
            "long_sl_mult": base_sl,
            "short_sl_mult": base_sl,
            "target_rr": DEFAULT_TARGET_RR,
            "trail_mult": DEFAULT_TRAIL_MULT,
            "trail_arm_atr": DEFAULT_TRAIL_ARM_ATR,
            "pause": False,
            "reason": "insufficient returns — using defaults",
            "stats": {},
        }

    skew = _safe_float(returns.skew())
    kurt = _safe_float(returns.kurtosis())
    up = returns[returns > 0]
    dn = returns[returns < 0]
    up_std = _safe_float(up.std(), 0.0)
    dn_std = _safe_float(dn.std(), 0.0)

    # Asymmetric stop multipliers, derived from skew.
    #   - negative skew (down-tails dominant, e.g. Brent): longs need MORE room
    #     (noise stops them out); shorts need TIGHTER stops (fast profit moves).
    #   - positive skew (up-tails, e.g. HYPE): opposite.
    # Scale kept modest so we don't blow up the stop distance on extreme skew.
    long_sl = round(base_sl * (1.0 - skew * 0.1), 3)
    short_sl = round(base_sl * (1.0 + skew * 0.1), 3)
    long_sl = max(0.3, min(long_sl, 3.0))
    short_sl = max(0.3, min(short_sl, 3.0))

    # Regime / distribution shift detection
    historical = returns[:-RECENT_N] if len(returns) > RECENT_N + 20 else returns
    recent = returns[-RECENT_N:] if len(returns) > RECENT_N + 20 else returns
    hist_std = _safe_float(historical.std(), 1e-9)
    hist_mean = _safe_float(historical.mean(), 0.0)
    rec_std = _safe_float(recent.std(), 0.0)
    rec_mean = _safe_float(recent.mean(), 0.0)
    vol_shift = rec_std / hist_std if hist_std > 1e-12 else 1.0
    mean_shift_std = (rec_mean - hist_mean) / hist_std if hist_std > 1e-12 else 0.0

    # PAUSE flag — only triggered on genuinely extreme regime shifts so we don't
    # stop trading at the first sign of a lull. Thresholds tuned so normal chop
    # days stay TRADEABLE and only structural breaks trigger PAUSE.
    pause = False
    reason_parts = []
    if vol_shift > 2.2:
        pause = True
        reason_parts.append(f"vol_shift={vol_shift:.2f}x (volatility explosion)")
    if vol_shift < 0.30:
        pause = True
        reason_parts.append(f"vol_shift={vol_shift:.2f}x (vol collapse — momentum dead)")
    if abs(mean_shift_std) > 1.8:
        pause = True
        reason_parts.append(f"mean_shift={mean_shift_std:+.2f}σ (structural drift)")

    # Target R:R: more asymmetric distribution → bigger targets
    tail_ratio = max(up_std, dn_std) / max(min(up_std, dn_std), 1e-9)
    if tail_ratio > 1.5:
        target_rr = 2.5
    elif tail_ratio > 1.2:
        target_rr = 2.2
    else:
        target_rr = 2.0

    profile = {
        "long_sl_mult": long_sl,
        "short_sl_mult": short_sl,
        "target_rr": target_rr,
        "trail_mult": DEFAULT_TRAIL_MULT,
        "trail_arm_atr": DEFAULT_TRAIL_ARM_ATR,
        "pause": pause,
        "reason": "; ".join(reason_parts) if reason_parts else "stable",
        "stats": {
            "skew": round(skew, 3),
            "kurtosis": round(kurt, 2),
            "vol_shift": round(vol_shift, 3),
            "mean_shift_std": round(mean_shift_std, 3),
            "tail_ratio": round(tail_ratio, 3),
            "up_std": round(up_std, 6),
            "dn_std": round(dn_std, 6),
            "n_bars": int(len(returns)),
        },
    }
    return profile


def build_and_write(symbols: list[str], fetcher: Callable[[str], pd.DataFrame],
                    output_path: str, base_sl_per_symbol: Dict[str, float] = None) -> Dict[str, Any]:
    """Compute profiles for each symbol and write the adaptive_stops.json file.

    `fetcher(symbol)` must return a pandas DataFrame with at least a 'close' column.
    `base_sl_per_symbol` lets each bot pass its per-symbol baseline ATR stop mult
    (falls back to DEFAULT_BASE_SL when missing).
    """
    base_sl_per_symbol = base_sl_per_symbol or {}
    profiles: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for sym in symbols:
        try:
            df = fetcher(sym)
            base = base_sl_per_symbol.get(sym, DEFAULT_BASE_SL)
            profiles[sym] = compute_profile(sym, df, base_sl=base)
        except Exception as e:
            errors[sym] = str(e)
            profiles[sym] = {
                "long_sl_mult": base_sl_per_symbol.get(sym, DEFAULT_BASE_SL),
                "short_sl_mult": base_sl_per_symbol.get(sym, DEFAULT_BASE_SL),
                "target_rr": DEFAULT_TARGET_RR,
                "trail_mult": DEFAULT_TRAIL_MULT,
                "trail_arm_atr": DEFAULT_TRAIL_ARM_ATR,
                "pause": False,
                "reason": f"compute error: {e}",
                "stats": {},
            }

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": "shared/adaptive_stops.py",
        "symbols": profiles,
        "errors": errors,
    }
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, output_path)
    return payload


def load_profile(output_path: str, max_age_hours: int = 48) -> Dict[str, Any]:
    """Read adaptive_stops.json and return {symbol: profile}. Empty dict if missing/stale."""
    try:
        if not os.path.exists(output_path):
            return {}
        with open(output_path) as f:
            data = json.load(f)
        gen = data.get("generated_at", "")
        if gen:
            try:
                stamp = datetime.fromisoformat(gen.rstrip("Z"))
                age_h = (datetime.utcnow() - stamp).total_seconds() / 3600.0
                if age_h > max_age_hours:
                    return {}
            except Exception:
                pass
        return data.get("symbols", {}) or {}
    except Exception:
        return {}
