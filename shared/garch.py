"""GARCH(1,1) volatility forecasting — shared between commodities-bot and crypto-bot.

Ported and generalised from commodities-bot/research/backtest_garch_sizing.py.

The rolling 252-bar fitting window matches the master CLAUDE.md spec. For fast
paths the `window` argument lets callers shrink the fitting history.

Usage:
    from shared import garch
    forecast = garch.forecast_vol(close_prices, window=252)
    size_pct = garch.dynamic_position_size(0.20, close_prices)
"""
from __future__ import annotations

import warnings
from typing import Literal

import numpy as np
import pandas as pd

try:
    from arch import arch_model
    from arch.univariate.base import ARCHModelResult
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The `arch` package is required. Install with `pip install arch`."
    ) from exc


DEFAULT_WINDOW = 252
_ANNUALISATION_SCALE = 1.0  # returns are already in percent in this module


def _returns_pct(prices: pd.Series) -> pd.Series:
    """Log returns expressed as percent (matches the arch library convention)."""
    return np.log(prices / prices.shift(1)).dropna() * 100.0


def fit_garch(prices: pd.Series, window: int = DEFAULT_WINDOW) -> ARCHModelResult:
    """Fit a GARCH(1,1) model on the last `window` returns.

    Args:
        prices: Close-price series (pandas).
        window: Number of most-recent returns to use for fitting. Defaults to 252.

    Returns:
        The fitted `ARCHModelResult`. Raises ValueError if not enough data.
    """
    returns = _returns_pct(prices).tail(window)
    if len(returns) < 50:
        raise ValueError(
            f"GARCH(1,1) needs at least 50 returns to converge (got {len(returns)})"
        )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = arch_model(returns, vol="GARCH", p=1, q=1, dist="normal")
        return model.fit(disp="off")


def forecast_vol(
    prices: pd.Series,
    horizon: int = 1,
    window: int = DEFAULT_WINDOW,
) -> float:
    """Forecast the next `horizon` bars of volatility (annualised-equivalent %).

    Returns 0.0 on convergence failure or insufficient data (caller should fall
    back to static sizing rather than crashing).
    """
    try:
        res = fit_garch(prices, window=window)
    except (ValueError, Exception):  # pragma: no cover — arch raises many types
        return 0.0

    try:
        forecast = res.forecast(horizon=horizon, reindex=False)
        variance = forecast.variance.values[-1, horizon - 1]
        return float(np.sqrt(variance) * _ANNUALISATION_SCALE)
    except Exception:  # pragma: no cover
        return 0.0


def classify_regime(
    prices: pd.Series,
    window: int = DEFAULT_WINDOW,
    threshold_pct: int = 75,
) -> Literal["high_vol", "low_vol"]:
    """Classify the current volatility regime.

    Returns "high_vol" if the GARCH forecast exceeds the `threshold_pct`
    percentile of the historical rolling-window volatility, else "low_vol".
    """
    forecast = forecast_vol(prices, window=window)
    if forecast == 0.0:
        return "low_vol"

    returns = _returns_pct(prices)
    rolling_vol = returns.rolling(window=min(20, len(returns))).std().dropna()
    if rolling_vol.empty:
        return "low_vol"

    cutoff = float(np.percentile(rolling_vol.values, threshold_pct))
    return "high_vol" if forecast > cutoff else "low_vol"


def dynamic_stop_mult(
    base_mult: float,
    prices: pd.Series,
    window: int = DEFAULT_WINDOW,
) -> float:
    """Scale the base ATR multiplier by the GARCH forecast / historical mean ratio.

    High forecast vol → widen stop (up to 1.5x base).
    Low forecast vol → tighten stop (down to 0.7x base).
    Convergence failures return `base_mult` unchanged.
    """
    forecast = forecast_vol(prices, window=window)
    if forecast == 0.0:
        return base_mult

    returns = _returns_pct(prices)
    mean_vol = float(returns.tail(window).std())
    if mean_vol <= 0:
        return base_mult

    ratio = forecast / mean_vol
    scaled = base_mult * ratio
    return float(np.clip(scaled, base_mult * 0.7, base_mult * 1.5))


def dynamic_position_size(
    base_pct: float,
    prices: pd.Series,
    window: int = DEFAULT_WINDOW,
    floor: float = 0.10,
    ceil: float = 0.30,
) -> float:
    """Scale the base position size inversely to the GARCH forecast.

    High forecast vol → smaller position.
    Low forecast vol → larger position.
    Bounded to [floor, ceil]. Falls back to `base_pct` on convergence failure.
    """
    forecast = forecast_vol(prices, window=window)
    if forecast == 0.0:
        return base_pct

    returns = _returns_pct(prices)
    mean_vol = float(returns.tail(window).std())
    if mean_vol <= 0:
        return base_pct

    # Inverse scaling — high vol shrinks size, low vol grows it.
    inv_ratio = mean_vol / forecast
    scaled = base_pct * inv_ratio
    return float(np.clip(scaled, floor, ceil))


if __name__ == "__main__":  # pragma: no cover
    import yfinance as yf

    print("Fetching XAUUSD=X (gold) daily close from yfinance...")
    df = yf.download("XAUUSD=X", period="2y", interval="1d", progress=False)
    if df.empty:
        raise SystemExit("No data — is yfinance reachable?")

    close = df["Close"].squeeze()
    print(f"  {len(close)} bars loaded")

    fv = forecast_vol(close)
    regime = classify_regime(close)
    stop_mult = dynamic_stop_mult(0.7, close)
    pos_pct = dynamic_position_size(0.20, close)

    print(f"\nGARCH(1,1) snapshot (window={DEFAULT_WINDOW}):")
    print(f"  forecast vol     {fv:.4f}%")
    print(f"  regime           {regime}")
    print(f"  dynamic stop     0.70x → {stop_mult:.3f}x")
    print(f"  dynamic size     20% → {pos_pct * 100:.1f}%")
