"""Unified regime classifier: Shannon entropy + GARCH(1,1).

- Low entropy  → ordered / trending market (good for momentum)
- High entropy → disordered / choppy market (no edge for momentum)
- GARCH vol classifies how risky that regime is

Combined output tells a momentum strategy whether to trade, reduce size, or skip.
"""
from __future__ import annotations

from typing import Literal, Tuple

import numpy as np
import pandas as pd

from . import garch


Regime = Literal["trend", "chop", "volatile", "quiet"]
Recommendation = Literal["trade", "reduce_size", "skip"]


def shannon_entropy(
    prices: pd.Series,
    window: int = 20,
    bins: int = 10,
) -> pd.Series:
    """Rolling Shannon entropy of log returns.

    High values → returns are spread across many bins (disordered).
    Low values → returns concentrated in few bins (ordered/trending).

    The returned series is NaN until `window` bars of returns are available.
    """
    returns = np.log(prices / prices.shift(1)).dropna()

    def _entropy_at(window_vals: np.ndarray) -> float:
        if len(window_vals) < 2:
            return np.nan
        hist, _ = np.histogram(window_vals, bins=bins)
        p = hist / hist.sum() if hist.sum() > 0 else hist
        p = p[p > 0]
        return float(-np.sum(p * np.log(p))) if len(p) else 0.0

    rolled = returns.rolling(window=window).apply(_entropy_at, raw=True)
    return rolled.reindex(prices.index)


def _entropy_threshold(entropy_series: pd.Series, pct: int = 60) -> float:
    """High-entropy cutoff = given percentile of the series. NaN-safe."""
    clean = entropy_series.dropna()
    if clean.empty:
        return float("inf")
    return float(np.percentile(clean.values, pct))


def combined_regime(
    prices: pd.Series,
    garch_window: int = 252,
    entropy_window: int = 20,
) -> dict:
    """Classify the most recent bar using GARCH + Shannon entropy.

    Returns a dict with regime, vol + entropy diagnostics, and a recommendation.
    """
    forecast = garch.forecast_vol(prices, window=garch_window)
    garch_regime = garch.classify_regime(prices, window=garch_window)

    entropy_series = shannon_entropy(prices, window=entropy_window)
    entropy_now = float(entropy_series.dropna().iloc[-1]) if not entropy_series.dropna().empty else float("nan")
    threshold = _entropy_threshold(entropy_series, pct=60)
    entropy_regime = "high_entropy" if entropy_now > threshold else "low_entropy"

    if entropy_regime == "low_entropy" and garch_regime == "low_vol":
        regime: Regime = "quiet"
        recommendation: Recommendation = "trade"
        confidence = 0.9
    elif entropy_regime == "low_entropy" and garch_regime == "high_vol":
        regime = "volatile"
        recommendation = "reduce_size"
        confidence = 0.6
    elif entropy_regime == "high_entropy" and garch_regime == "low_vol":
        regime = "chop"
        recommendation = "skip"
        confidence = 0.8
    else:
        regime = "volatile"
        recommendation = "skip"
        confidence = 0.85

    return {
        "regime": regime,
        "garch_vol": forecast,
        "garch_regime": garch_regime,
        "entropy": entropy_now,
        "entropy_regime": entropy_regime,
        "confidence": confidence,
        "recommendation": recommendation,
    }


# Instrument-specific thresholds applied on top of combined_regime.
_INSTRUMENT_RULES = {
    # Commodities-bot xyz symbols
    "xyz:GOLD":     {"min_slope_pct": 0.1},
    "xyz:SILVER":   {"min_atr_ratio": 0.8},
    "xyz:BRENTOIL": {"min_adx": 18.0, "max_atr_ratio": 1.5},
    "xyz:NATGAS":   {"min_slope_pct": 0.1},
    "xyz:COPPER":   {"min_slope_pct": 0.1},
    # Crypto-bot native perps
    "ETH":          {"max_atr_ratio": 1.5},
    "HYPE":         {"max_atr_ratio": 1.5},
    # Legacy OANDA tickers (in case research scripts use them)
    "XAUUSD":       {"min_slope_pct": 0.1},
    "XAGUSD":       {"min_atr_ratio": 0.8},
    "BCOUSD":       {"min_adx": 18.0, "max_atr_ratio": 1.5},
}


def _slope_pct(prices: pd.Series, window: int = 50) -> float:
    """Percent slope of a simple linear fit over the last `window` bars."""
    tail = prices.tail(window).dropna()
    if len(tail) < 2:
        return 0.0
    x = np.arange(len(tail))
    slope = np.polyfit(x, tail.values, 1)[0]
    return float(slope / tail.mean() * 100)  # percent


def _atr_ratio(prices: pd.Series, window: int = 14) -> float:
    """Current ATR-proxy (stdev of returns) vs its rolling mean."""
    returns = np.log(prices / prices.shift(1)).dropna()
    if len(returns) < window * 2:
        return 1.0
    atr_now = float(returns.tail(window).std())
    atr_mean = float(returns.rolling(window).std().dropna().mean())
    return atr_now / atr_mean if atr_mean > 0 else 1.0


def should_trade(prices: pd.Series, instrument: str) -> Tuple[bool, str]:
    """Apply instrument-specific gates on top of the combined regime.

    Returns (ok_to_trade, reason_string).
    """
    regime = combined_regime(prices)

    if regime["recommendation"] == "skip":
        return False, f"combined regime says skip ({regime['regime']})"

    rules = _INSTRUMENT_RULES.get(instrument, {})

    if "min_slope_pct" in rules:
        slope = _slope_pct(prices)
        if abs(slope) < rules["min_slope_pct"]:
            return False, f"{instrument}: weak trend slope {slope:.3f}%"

    atr_ratio = _atr_ratio(prices) if ("min_atr_ratio" in rules or "max_atr_ratio" in rules) else None
    if atr_ratio is not None:
        if "min_atr_ratio" in rules and atr_ratio < rules["min_atr_ratio"]:
            return False, f"{instrument}: low vol (ATR ratio {atr_ratio:.2f})"
        if "max_atr_ratio" in rules and atr_ratio > rules["max_atr_ratio"]:
            return False, f"{instrument}: extreme vol (ATR ratio {atr_ratio:.2f})"

    return True, f"{instrument}: {regime['regime']} regime, {regime['recommendation']}"


if __name__ == "__main__":  # pragma: no cover
    import yfinance as yf

    df = yf.download("XAUUSD=X", period="2y", interval="1d", progress=False)
    close = df["Close"].squeeze()

    result = combined_regime(close)
    print("Combined regime for XAUUSD (most recent bar):")
    for k, v in result.items():
        print(f"  {k:18s} {v}")

    ok, reason = should_trade(close, "xyz:GOLD")
    print(f"\nshould_trade(xyz:GOLD) → {ok}  ({reason})")
