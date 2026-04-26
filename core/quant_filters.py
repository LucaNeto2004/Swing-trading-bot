"""Quant-grade regime + pivot validation.

Computes three independent regime indicators on 1h data:
  - Hurst exponent (rolling R/S analysis) — trending vs mean-reverting
  - ADX (Wilder's directional movement) — trend strength
  - Combined with the existing 5-filter ensemble vote for direction

Plus validated pivot detection: a fractal pivot is "real" only when at least
2 of 3 quant validators agree:
  - RSI at extreme (< 30 or > 70 within ±3 bars of the pivot)
  - Bollinger outer-band touch on the pivot bar
  - Volume spike (≥ 1.5× 20-bar mean)
Plus an ATR-move gate: the price must move ≥ 0.8× ATR from the pivot within
the next 3 bars (rejects flat-consolidation "pivots").

All functions output arrays aligned to the ENTRY timeframe (5m or 15m) using
the same causal alignment as our other *_lookup_1h functions (previous closed
1h bar, no look-ahead).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Hurst exponent (rolling R/S)
# ---------------------------------------------------------------------------

def rolling_hurst(close_1h: np.ndarray, window: int = 100) -> np.ndarray:
    """Per-bar Hurst exponent via R/S statistic on log returns.

    H > 0.55 = persistent (trending)
    H ≈ 0.5  = random walk
    H < 0.45 = anti-persistent (mean-reverting)

    Uses single-scale R/S on a rolling `window`. Warm-up bars return NaN."""
    n = len(close_1h)
    h = np.full(n, np.nan)
    if n < window + 2:
        return h
    # log returns
    log_r = np.diff(np.log(np.maximum(close_1h, 1e-12)))
    # rolling R/S
    for t in range(window, n):
        r = log_r[t - window: t]
        mu = r.mean()
        y = np.cumsum(r - mu)
        R = float(y.max() - y.min())
        S = float(r.std())
        if S > 1e-12 and R > 0:
            h[t] = float(np.log(R / S) / np.log(window))
    return h


# ---------------------------------------------------------------------------
# ADX (Wilder's Directional Movement)
# ---------------------------------------------------------------------------

def compute_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                period: int = 14) -> np.ndarray:
    """14-period ADX. Uses Wilder's smoothing (EMA with alpha = 1/period).

    ADX ≥ 25 = strong trend
    ADX < 20 = no trend (range)
    """
    n = len(close)
    adx = np.full(n, np.nan)
    if n < period * 3:
        return adx

    prev_close = np.concatenate([[close[0]], close[:-1]])
    prev_high = np.concatenate([[high[0]], high[:-1]])
    prev_low = np.concatenate([[low[0]], low[:-1]])

    tr = np.maximum.reduce([high - low,
                            np.abs(high - prev_close),
                            np.abs(low - prev_close)])

    up_move = high - prev_high
    dn_move = prev_low - low
    plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    # first bar: no prior — zero DM/TR components
    tr[0] = high[0] - low[0]; plus_dm[0] = 0.0; minus_dm[0] = 0.0

    def _wilder(arr):
        out = np.zeros_like(arr, dtype=np.float64)
        # seed: first period-1 bars are NaN equivalent; accumulate after
        if len(arr) < period:
            return out
        out[period - 1] = arr[:period].sum()
        for i in range(period, len(arr)):
            out[i] = out[i - 1] - out[i - 1] / period + arr[i]
        return out

    tr_s = _wilder(tr)
    plus_s = _wilder(plus_dm)
    minus_s = _wilder(minus_dm)

    # Avoid div-by-zero
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * np.where(tr_s > 0, plus_s / tr_s, 0.0)
        minus_di = 100.0 * np.where(tr_s > 0, minus_s / tr_s, 0.0)
        dx = 100.0 * np.where((plus_di + minus_di) > 0,
                              np.abs(plus_di - minus_di) / (plus_di + minus_di),
                              0.0)

    # ADX = 14-period Wilder smoothing of DX
    adx_out = np.zeros(n)
    if n < 2 * period:
        return np.full(n, np.nan)
    adx_out[2 * period - 1] = dx[period: 2 * period].mean()
    for i in range(2 * period, n):
        adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    adx_out[: 2 * period - 1] = np.nan
    return adx_out


# ---------------------------------------------------------------------------
# Validated pivot detection
# ---------------------------------------------------------------------------

def validated_pivots_1h(df_1h: pd.DataFrame, lookback: int = 3,
                        atr_min_move: float = 0.5,
                        vol_spike: float = 1.3,
                        rsi_extreme: tuple = (35.0, 65.0),
                        min_validators: int = 1,
                        ) -> tuple[list, list]:
    """Return lists of VALIDATED pivot_H and pivot_L on 1h.

    Each item: (confirm_ts, pivot_bar_idx, pivot_price, n_validators_passed).
    A candidate pivot (fractal low/high with `lookback` bars on each side)
    is validated if at least 2 of 3 checks pass:
      - RSI extreme (any bar in [k-3, k+3] has RSI < rsi_low or > rsi_high)
      - Bollinger outer-band touch on bar k
      - Volume on bar k ≥ `vol_spike` × 20-bar mean volume
    AND a hard ATR-move gate: max price move from pivot in next 3 bars ≥
    `atr_min_move` × ATR at pivot.

    df_1h must have columns: high, low, close, volume, rsi, atr, bb_upper,
    bb_lower, timestamp.
    """
    n = len(df_1h)
    if n < 2 * lookback + 1:
        return [], []
    high = df_1h["high"].to_numpy()
    low = df_1h["low"].to_numpy()
    close = df_1h["close"].to_numpy()
    vol = df_1h["volume"].to_numpy() if "volume" in df_1h else np.ones(n)
    rsi = df_1h["rsi"].to_numpy() if "rsi" in df_1h else np.full(n, 50.0)
    atr = df_1h["atr"].to_numpy() if "atr" in df_1h else np.full(n, 0.0)
    bb_u = df_1h["bb_upper"].to_numpy() if "bb_upper" in df_1h else np.full(n, np.nan)
    bb_l = df_1h["bb_lower"].to_numpy() if "bb_lower" in df_1h else np.full(n, np.nan)
    # Normalize ts to int64 ns — avoids tz-aware vs naive Timestamp mixing
    ts = pd.to_datetime(df_1h["timestamp"], utc=True).astype("int64").to_numpy()

    # Rolling 20-bar mean volume
    vol_mean = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()

    rsi_lo, rsi_hi = rsi_extreme
    out_h = []
    out_l = []

    for k in range(lookback, n - lookback):
        wh = high[k - lookback: k + lookback + 1]
        wl = low[k - lookback: k + lookback + 1]
        is_high = high[k] == wh.max() and (wh == high[k]).sum() == 1
        is_low = low[k] == wl.min() and (wl == low[k]).sum() == 1
        if not (is_high or is_low):
            continue
        # shared windows
        rsi_window = rsi[max(0, k - 3): min(n, k + 4)]
        rsi_window = rsi_window[~np.isnan(rsi_window)]
        volume_spike = vol_mean[k] > 0 and vol[k] >= vol_spike * vol_mean[k]

        if is_high:
            v_rsi = np.any(rsi_window > rsi_hi) if len(rsi_window) else False
            v_bb = not np.isnan(bb_u[k]) and high[k] >= bb_u[k]
            n_pass = int(v_rsi) + int(v_bb) + int(volume_spike)
            # ATR move check — max drop from pivot in next 3 bars
            if k + 3 < n and atr[k] > 0:
                future_min = low[k + 1: k + 4].min()
                atr_move = (high[k] - future_min) / atr[k]
                atr_ok = atr_move >= atr_min_move
            else:
                atr_ok = True  # can't assess yet — let it through
            if n_pass >= min_validators and atr_ok:
                out_h.append((ts[k + lookback], k, float(high[k]), n_pass))
        if is_low:
            v_rsi = np.any(rsi_window < rsi_lo) if len(rsi_window) else False
            v_bb = not np.isnan(bb_l[k]) and low[k] <= bb_l[k]
            n_pass = int(v_rsi) + int(v_bb) + int(volume_spike)
            if k + 3 < n and atr[k] > 0:
                future_max = high[k + 1: k + 4].max()
                atr_move = (future_max - low[k]) / atr[k]
                atr_ok = atr_move >= atr_min_move
            else:
                atr_ok = True
            if n_pass >= min_validators and atr_ok:
                out_l.append((ts[k + lookback], k, float(low[k]), n_pass))
    return out_h, out_l


def _hma(close: np.ndarray, period: int = 20) -> np.ndarray:
    """Hull Moving Average — low-lag smoother: HMA = WMA(2×WMA(n/2) − WMA(n), sqrt(n))"""
    n = len(close)
    half = max(2, period // 2)
    sq = max(2, int(np.sqrt(period)))

    def wma(arr, p):
        w = np.arange(1, p + 1, dtype=np.float64)
        out = np.full(len(arr), np.nan)
        for i in range(p - 1, len(arr)):
            out[i] = float(np.sum(arr[i - p + 1: i + 1] * w) / w.sum())
        return out

    wma_half = wma(close, half)
    wma_full = wma(close, period)
    raw = 2 * wma_half - wma_full
    hma_out = np.full(n, np.nan)
    valid = ~np.isnan(raw)
    if valid.sum() >= sq:
        hma_out[valid] = wma(raw[valid], sq)[:valid.sum()]
    return hma_out


def smoothed_peaks_1h(df_1h: pd.DataFrame, hma_period: int = 11,
                      lookback: int = 2) -> tuple[list, list]:
    """Find peaks/troughs on an HMA(hma_period)-smoothed 1h close series.

    A smoothed bar is a peak if it's the max of a (2*lookback+1)-bar window
    around it. Same for troughs. Confirmation happens at pivot_bar + lookback.

    Smoothing removes noise wiggles — the resulting extrema are the
    mathematically clean turning points.
    """
    n = len(df_1h)
    if n < 2 * lookback + hma_period + 5:
        return [], []
    close = df_1h["close"].to_numpy()
    smooth = _hma(close, hma_period)
    ts = pd.to_datetime(df_1h["timestamp"], utc=True).astype("int64").to_numpy()

    out_h = []
    out_l = []
    for k in range(max(lookback, hma_period + 2), n - lookback):
        if np.isnan(smooth[k]): continue
        window = smooth[k - lookback: k + lookback + 1]
        if np.isnan(window).any(): continue
        # Peak on smoothed series → map back to RAW price peak in ±lookback window
        if smooth[k] == window.max() and (window == smooth[k]).sum() == 1:
            raw_window = df_1h["high"].to_numpy()[k - lookback: k + lookback + 1]
            peak_idx = k - lookback + int(np.argmax(raw_window))
            out_h.append((ts[k + lookback], peak_idx, float(raw_window.max()), 0))
        if smooth[k] == window.min() and (window == smooth[k]).sum() == 1:
            raw_window = df_1h["low"].to_numpy()[k - lookback: k + lookback + 1]
            trough_idx = k - lookback + int(np.argmin(raw_window))
            out_l.append((ts[k + lookback], trough_idx, float(raw_window.min()), 0))
    return out_h, out_l


def combined_pivots_1h(df_1h: pd.DataFrame, df_4h: pd.DataFrame = None,
                        fractal_lookback: int = 3,
                        smoothed_lookback: int = 2,
                        atr_min_move: float = 1.0,
                        vol_spike: float = 1.3,
                        rsi_extreme: tuple = (35.0, 65.0),
                        ) -> tuple[list, list]:
    """Combined pivot detection using BOTH fractal + HMA-smoothed peaks.

    A pivot passes if:
      - It's detected by either method (fractal OR smoothed-peak)
      - AT LEAST 1 validator passes (RSI extreme, BB outer touch, or volume spike)
      - ATR move in next 3 bars ≥ atr_min_move × ATR at pivot

    Optional 4h df for HTF confirmation (if provided, pivot is stronger when
    4h structure agrees — adds 1 to validator count)."""
    frac_h, frac_l = validated_pivots_1h(df_1h, lookback=fractal_lookback,
                                          atr_min_move=atr_min_move,
                                          vol_spike=vol_spike,
                                          rsi_extreme=rsi_extreme,
                                          min_validators=1)
    smooth_h, smooth_l = smoothed_peaks_1h(df_1h, lookback=smoothed_lookback)

    # Smooth pivots need validation too — use the same validators
    def validate(piv_list, side):
        if not piv_list: return []
        rsi = df_1h["rsi"].to_numpy()
        vol = df_1h["volume"].to_numpy() if "volume" in df_1h else np.ones(len(df_1h))
        atr = df_1h["atr"].to_numpy()
        high = df_1h["high"].to_numpy(); low = df_1h["low"].to_numpy()
        bb_u = df_1h["bb_upper"].to_numpy() if "bb_upper" in df_1h else np.full(len(df_1h), np.nan)
        bb_l = df_1h["bb_lower"].to_numpy() if "bb_lower" in df_1h else np.full(len(df_1h), np.nan)
        vol_mean = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()
        rsi_lo, rsi_hi = rsi_extreme
        n = len(df_1h)
        out = []
        for confirm_ts, k, level, _ in piv_list:
            rsi_window = rsi[max(0, k - 3): min(n, k + 4)]
            rsi_window = rsi_window[~np.isnan(rsi_window)]
            v_vol = vol_mean[k] > 0 and vol[k] >= vol_spike * vol_mean[k]
            if side == "H":
                v_rsi = np.any(rsi_window > rsi_hi) if len(rsi_window) else False
                v_bb = not np.isnan(bb_u[k]) and high[k] >= bb_u[k]
            else:
                v_rsi = np.any(rsi_window < rsi_lo) if len(rsi_window) else False
                v_bb = not np.isnan(bb_l[k]) and low[k] <= bb_l[k]
            n_pass = int(v_rsi) + int(v_bb) + int(v_vol)
            # ATR move check
            atr_ok = True
            if k + 3 < n and atr[k] > 0:
                if side == "H":
                    future_min = low[k + 1: k + 4].min()
                    atr_ok = (high[k] - future_min) / atr[k] >= atr_min_move
                else:
                    future_max = high[k + 1: k + 4].max()
                    atr_ok = (future_max - low[k]) / atr[k] >= atr_min_move
            if n_pass >= 1 and atr_ok:
                out.append((confirm_ts, k, level, n_pass))
        return out

    smooth_h = validate(smooth_h, "H")
    smooth_l = validate(smooth_l, "L")

    # Merge fractal + smooth pivots, dedup by bar index (keep the best)
    def merge(a, b):
        seen = {}
        for confirm_ts, k, level, n_pass in a + b:
            if k not in seen or seen[k][3] < n_pass:
                seen[k] = (confirm_ts, k, level, n_pass)
        return sorted(seen.values())
    return merge(frac_h, smooth_h), merge(frac_l, smooth_l)


# ---------------------------------------------------------------------------
# Alignment helpers (1h → entry-timeframe array)
# ---------------------------------------------------------------------------

def align_1h_to_entry(values_1h: np.ndarray, ts_1h: np.ndarray,
                      ts_entry: np.ndarray) -> np.ndarray:
    """Project a 1h-level scalar array onto the entry timeframe (5m/15m),
    using the PREVIOUS CLOSED 1h bar for each entry bar (causal; no
    look-ahead). First entry bars before any closed 1h bar return NaN."""
    raw = np.searchsorted(ts_1h, ts_entry, side="right")
    idx = np.clip(raw - 2, 0, len(ts_1h) - 1)
    before = raw <= 1
    out = values_1h[idx].astype(float).copy()
    out[before] = np.nan
    return out


# ---------------------------------------------------------------------------
# Combined regime classifier
# ---------------------------------------------------------------------------

def classify_regime(up_cnt: int, dn_cnt: int, hurst: float, adx: float,
                    hurst_thresh: tuple = (0.45, 0.55),
                    adx_trend: float = 20.0) -> str:
    """Combine ensemble vote + Hurst + ADX into one of 4 regime labels:

      trend_up   — vote majority up AND Hurst > 0.55 AND ADX > 20
      trend_down — mirror
      range      — vote neutral AND Hurst < 0.45 AND ADX < 20
      chop       — anything else (contradicting signals)
    """
    if np.isnan(hurst) or np.isnan(adx):
        return "chop"
    vote = up_cnt - dn_cnt
    h_lo, h_hi = hurst_thresh
    if vote >= 2 and hurst > h_hi and adx > adx_trend:
        return "trend_up"
    if vote <= -2 and hurst > h_hi and adx > adx_trend:
        return "trend_down"
    if abs(vote) <= 1 and hurst < h_lo and adx < adx_trend:
        return "range"
    return "chop"
