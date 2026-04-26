"""Technical indicators + 1h trend precompute. Ported from whale_swing_backtest.ipynb."""
import numpy as np
import pandas as pd


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """EMAs, RSI, ATR, Bollinger Bands. Returns a new DataFrame with indicator columns."""
    df = df.copy()
    c = df['close']; h = df['high']; l = df['low']
    df['ema_9'] = c.ewm(span=9, adjust=False).mean()
    df['ema_21'] = c.ewm(span=21, adjust=False).mean()
    df['ema_50'] = c.ewm(span=50, adjust=False).mean()
    df['ema_200'] = c.ewm(span=200, adjust=False).mean()
    df['ema_50_slope'] = (df['ema_50'] - df['ema_50'].shift(20)) / df['ema_50'].shift(20)
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - 100 / (1 + rs)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1/14, adjust=False).mean()
    df['bb_mid'] = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df['bb_upper'] = df['bb_mid'] + 2 * bb_std
    df['bb_lower'] = df['bb_mid'] - 2 * bb_std
    return df


def _wma(x: np.ndarray, n: int) -> np.ndarray:
    """Linear-weighted moving average. weights 1..n, sum n(n+1)/2. NaN until n-1."""
    if n <= 1:
        return x.astype(float).copy()
    w = np.arange(1, n + 1, dtype=float)
    wsum = w.sum()
    out = np.full(len(x), np.nan, dtype=float)
    for i in range(n - 1, len(x)):
        out[i] = np.dot(x[i - n + 1 : i + 1], w) / wsum
    return out


def hma_slope_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                        length: int = 14, slope_lookback: int = 1,
                        min_slope_pct: float = 0.001) -> tuple:
    """Hull Moving Average slope-sign regime on the 1h timeframe.

    HMA(n) = WMA( 2*WMA(close, n/2) − WMA(close, n),  sqrt(n) ).
    Regime: up if HMA[i] − HMA[i − slope_lookback] > price[i] * min_slope_pct,
    down if the reverse, neither (both False) if the slope is inside the
    flat-zone threshold. No pivot confirmation → no multi-bar lag.

    Returns (up, dn) boolean arrays aligned to df_5m timestamps.
    """
    n = len(df_5m)
    if df_1h is None or df_1h.empty or len(df_1h) < max(length, slope_lookback + 2):
        u = np.ones(n, dtype=bool); d = np.ones(n, dtype=bool)
        return u, d
    closes = df_1h['close'].to_numpy(dtype=float)
    half = max(int(length // 2), 2)
    sqrt_n = max(int(round(np.sqrt(length))), 2)
    wma_half = _wma(closes, half)
    wma_full = _wma(closes, length)
    raw = 2.0 * wma_half - wma_full
    hma = _wma(raw, sqrt_n)
    # Slope relative to price, so threshold is scale-invariant across symbols
    slope = np.full_like(hma, np.nan)
    slope[slope_lookback:] = hma[slope_lookback:] - hma[:-slope_lookback]
    # Flat-zone threshold in absolute price terms — min_slope_pct of close
    flat = closes * min_slope_pct
    up_1h = np.zeros(len(df_1h), dtype=bool)
    dn_1h = np.zeros(len(df_1h), dtype=bool)
    valid = ~np.isnan(slope)
    up_1h[valid] = slope[valid] > flat[valid]
    dn_1h[valid] = slope[valid] < -flat[valid]

    ts_5m = df_5m['timestamp'].to_numpy()
    ts_1h = df_1h['timestamp'].to_numpy()
    # BIAS FIX (2026-04-22): use the last FULLY CLOSED 1h bar, not the bar
    # containing the current 5m bar. For a 5m bar at T, the in-progress 1h
    # bar's close value (when held in a static historical frame) is the price
    # at T+1h — i.e. future info. Shifting by -2 maps to the PREVIOUS completed
    # bar. Eliminates look-ahead bias that inflated regime/BOS backtests.
    raw = np.searchsorted(ts_1h, ts_5m, side='right')
    idx = np.clip(raw - 2, 0, len(ts_1h) - 1)
    up = up_1h[idx]
    dn = dn_1h[idx]
    before = raw <= 1  # no previous CLOSED 1h bar available yet
    up = np.where(before, True, up)
    dn = np.where(before, True, dn)
    return up, dn


def _align_1h_to_5m(flags_up_1h, flags_dn_1h, df_5m, df_1h):
    """Align per-1h-bar boolean arrays onto 5m timestamps via searchsorted.

    Uses the PREVIOUS fully-closed 1h bar (not the in-progress one) to avoid
    look-ahead bias in backtests. See BIAS FIX comment in hma_slope_lookup_1h.
    """
    ts_5m = df_5m['timestamp'].to_numpy()
    ts_1h = df_1h['timestamp'].to_numpy()
    raw = np.searchsorted(ts_1h, ts_5m, side='right')
    idx = np.clip(raw - 2, 0, len(ts_1h) - 1)
    up = flags_up_1h[idx]
    dn = flags_dn_1h[idx]
    before = raw <= 1
    up = np.where(before, True, up)
    dn = np.where(before, True, dn)
    return up, dn


def linreg_slope_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                           length: int = 20, min_slope_pct: float = 0.001) -> tuple:
    """Linear-regression slope of last `length` 1h closes. Slope sign defines
    regime. `min_slope_pct` (% of price) is a flat-zone threshold to reject
    slopes too small to call directional."""
    n5 = len(df_5m)
    if df_1h is None or df_1h.empty or len(df_1h) < length + 2:
        return np.ones(n5, dtype=bool), np.ones(n5, dtype=bool)
    closes = df_1h['close'].to_numpy(dtype=float)
    n = len(closes)
    slope_arr = np.full(n, np.nan, dtype=float)
    x = np.arange(length, dtype=float)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()
    for i in range(length - 1, n):
        y = closes[i - length + 1 : i + 1]
        y_mean = y.mean()
        slope_arr[i] = ((x - x_mean) * (y - y_mean)).sum() / x_var
    flat = closes * min_slope_pct
    up = np.zeros(n, dtype=bool); dn = np.zeros(n, dtype=bool)
    valid = ~np.isnan(slope_arr)
    up[valid] = slope_arr[valid] > flat[valid]
    dn[valid] = slope_arr[valid] < -flat[valid]
    return _align_1h_to_5m(up, dn, df_5m, df_1h)


def supertrend_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                         period: int = 10, multiplier: float = 3.0) -> tuple:
    """Classic Supertrend on 1h. Basis = (high + low) / 2 ± multiplier * ATR.
    Directional state flips when close crosses the basis. No pivot lag."""
    n5 = len(df_5m)
    if df_1h is None or df_1h.empty or 'atr' not in df_1h.columns or len(df_1h) < period + 2:
        return np.ones(n5, dtype=bool), np.ones(n5, dtype=bool)
    high = df_1h['high'].to_numpy(dtype=float)
    low = df_1h['low'].to_numpy(dtype=float)
    close = df_1h['close'].to_numpy(dtype=float)
    atr = df_1h['atr'].to_numpy(dtype=float)
    atr = np.where(np.isnan(atr), 0.0, atr)
    n = len(close)
    hl2 = (high + low) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = np.zeros(n, dtype=int)  # +1 up, -1 down
    direction[0] = 1
    for i in range(1, n):
        final_upper[i] = upper[i] if (upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]) else final_upper[i-1]
        final_lower[i] = lower[i] if (lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]) else final_lower[i-1]
        if direction[i-1] == 1:
            direction[i] = -1 if close[i] < final_lower[i] else 1
        else:
            direction[i] = 1 if close[i] > final_upper[i] else -1
    up = direction > 0
    dn = direction < 0
    return _align_1h_to_5m(up, dn, df_5m, df_1h)


def price_vs_ema_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                           ema_col: str = 'ema_21') -> tuple:
    """Simplest possible: is 1h close above (up) or below (dn) the 1h EMA?
    Zero smoothing of the regime signal — reacts the instant price crosses."""
    n5 = len(df_5m)
    if df_1h is None or df_1h.empty or ema_col not in df_1h.columns:
        return np.ones(n5, dtype=bool), np.ones(n5, dtype=bool)
    c = df_1h['close'].to_numpy(dtype=float)
    e = df_1h[ema_col].to_numpy(dtype=float)
    up = c > e
    dn = c < e
    return _align_1h_to_5m(up, dn, df_5m, df_1h)


def _sjm_fit(X: np.ndarray, lam: float, K: int = 2, max_iter: int = 50, tol: float = 1e-6):
    """Fit Statistical Jump Model per Shu, Yu, Mulvey (2024, J. Asset Management).

    Minimizes:  Σ_t ||x_t − θ_{z_t}||² + λ · Σ_t 1[z_t ≠ z_{t−1}]

    Coordinate descent:
      (1) fix θ → Z update via dynamic programming (Viterbi over K states)
      (2) fix Z → θ_k = centroid of x_t where z_t = k  (k-means step)
    Repeat until assignments stop changing.

    Returns (Z, theta) where Z is int array [N] and theta is float array [K, D].
    """
    N, D = X.shape
    # k-means++ init for θ
    rng = np.random.default_rng(0)
    idx0 = rng.integers(N)
    centers = [X[idx0]]
    for _ in range(1, K):
        d2 = np.min(
            np.stack([((X - c) ** 2).sum(axis=1) for c in centers], axis=0),
            axis=0,
        )
        p = d2 / (d2.sum() + 1e-12)
        centers.append(X[rng.choice(N, p=p)])
    theta = np.stack(centers, axis=0)

    Z_prev = np.zeros(N, dtype=int)
    for it in range(max_iter):
        # ---- DP Z-update (Viterbi with linear jump penalty) ----
        cost = np.stack([((X - theta[k]) ** 2).sum(axis=1) for k in range(K)], axis=1)  # [N, K]
        V = np.empty((N, K), dtype=float)
        back = np.empty((N, K), dtype=int)
        V[0] = cost[0]
        back[0] = 0
        for t in range(1, N):
            # for each current state k, choose best previous state j
            prev = V[t - 1][:, None] + lam * (1 - np.eye(K))  # [K_prev, K_cur]
            jmin = prev.argmin(axis=0)  # [K_cur]
            V[t] = cost[t] + prev[jmin, np.arange(K)]
            back[t] = jmin
        # backtrack
        Z = np.empty(N, dtype=int)
        Z[-1] = V[-1].argmin()
        for t in range(N - 2, -1, -1):
            Z[t] = back[t + 1, Z[t + 1]]

        # ---- θ update (k-means step) ----
        for k in range(K):
            mask = Z == k
            if mask.sum() > 0:
                theta[k] = X[mask].mean(axis=0)

        if np.array_equal(Z, Z_prev):
            break
        Z_prev = Z

    return Z, theta


def _sjm_features(closes: np.ndarray, window: int = 6) -> np.ndarray:
    """Build the SJM feature matrix from 1h closes: per-bar returns + rolling
    downside deviation + rolling realized vol. Standardized (zero-mean, unit-var)."""
    r = np.zeros(len(closes), dtype=float)
    r[1:] = np.log(closes[1:] / closes[:-1])
    # rolling vol and downside dev
    vol = np.zeros_like(r)
    dsd = np.zeros_like(r)
    for i in range(len(r)):
        j0 = max(0, i - window + 1)
        w = r[j0 : i + 1]
        vol[i] = w.std(ddof=0) if len(w) > 1 else 0.0
        neg = w[w < 0]
        dsd[i] = neg.std(ddof=0) if len(neg) > 1 else 0.0
    # Rolling mean return (directional signal)
    mu = np.zeros_like(r)
    for i in range(len(r)):
        j0 = max(0, i - window + 1)
        mu[i] = r[j0 : i + 1].mean()
    X = np.stack([mu, vol, dsd], axis=1)
    # Standardize
    mean = X.mean(axis=0)
    std = X.std(axis=0, ddof=0) + 1e-12
    return (X - mean) / std


def sjm_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                  lam: float = 30.0, window: int = 6,
                  train_frac: float = 0.7) -> tuple:
    """SJM-based 1h regime filter.

    Fit SJM on the first `train_frac` of 1h bars, freeze θ, then predict
    regimes causally on the rest of the series. "Bull" regime = centroid with
    higher mean-return feature → allows longs; "Bear" regime allows shorts.

    Causal: once θ is frozen, each bar's assignment only depends on its own
    features and the previous bar's regime — no look-ahead.

    Returns (up, dn) boolean arrays aligned to df_5m timestamps.
    """
    n5 = len(df_5m)
    if df_1h is None or df_1h.empty or len(df_1h) < max(50, 2 * window):
        return np.ones(n5, dtype=bool), np.ones(n5, dtype=bool)
    closes = df_1h['close'].to_numpy(dtype=float)
    X = _sjm_features(closes, window=window)
    n1 = len(X)
    train_end = max(int(n1 * train_frac), 30)
    Z_train, theta = _sjm_fit(X[:train_end], lam=lam, K=2)

    # Identify which centroid is "bull" — higher mean-return feature (col 0)
    bull = 0 if theta[0, 0] > theta[1, 0] else 1
    bear = 1 - bull

    # Causal sequential assignment with frozen θ on the OOS tail
    Z_full = np.empty(n1, dtype=int)
    Z_full[: train_end] = Z_train
    for t in range(train_end, n1):
        prev = Z_full[t - 1]
        costs = ((X[t] - theta) ** 2).sum(axis=1).copy()
        for k in range(2):
            if k != prev:
                costs[k] += lam
        Z_full[t] = int(costs.argmin())

    up_1h = (Z_full == bull)
    dn_1h = (Z_full == bear)
    return _align_1h_to_5m(up_1h, dn_1h, df_5m, df_1h)


def trend_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> tuple:
    """For each 5m bar, return (up_ok, dn_ok) boolean arrays based on the
    most recent closed 1h bar's ema_21 vs ema_50. Uses searchsorted — O(log n)."""
    n = len(df_5m)
    if df_1h is None or df_1h.empty:
        return np.ones(n, dtype=bool), np.ones(n, dtype=bool)
    ts_5m = df_5m['timestamp'].to_numpy()
    ts_1h = df_1h['timestamp'].to_numpy()
    e21 = df_1h['ema_21'].to_numpy()
    e50 = df_1h['ema_50'].to_numpy()
    # BIAS FIX: use last CLOSED 1h bar (see note in hma_slope_lookup_1h).
    raw = np.searchsorted(ts_1h, ts_5m, side='right')
    idx = np.clip(raw - 2, 0, len(ts_1h) - 1)
    up = e21[idx] > e50[idx]
    dn = e21[idx] < e50[idx]
    before = raw <= 1
    up = np.where(before, True, up)
    dn = np.where(before, True, dn)
    return up, dn


def structure_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                        pivot_bars: int = 5,
                        min_choch_displacement_atr: float = 0.5,
                        min_bos_displacement_atr: float = 0.3,
                        min_choch_displacement_pct: float = 0.003,   # 0.3% price floor
                        min_bos_displacement_pct: float = 0.002,     # 0.2% price floor
                        confirmation_bars: int = 1,                  # 1 = next-bar confirms
                        min_hold_bars: int = 6,                      # cooldown between flips
                        *, return_bos: bool = False) -> tuple:
    """ICT-style 1h structure filter — state machine with BOS + CHoCH.

    Maintains a trend state ('up' | 'down' | 'unknown') that PERSISTS once
    established. Transitions:

      BOS (Break of Structure, continuation):
        - In 'up': a new confirmed pivot-high exceeds the previous protected high
        - In 'down': a new confirmed pivot-low breaks the previous protected low
        (updates protected level, trend stays the same)

      CHoCH (Change of Character, reversal):
        - In 'up': close CLOSES below the protected swing low
        - In 'down': close CLOSES above the protected swing high
        (flips trend state; establishes a new protected level from the
        most-recent confirmed pivot on the new-trend side)

    Why this is better than classical Dow:
      - Doesn't go "neutral" on every HH/HL hiccup — rides through noise
      - Flip only happens on a decisive CLOSE through the protected swing
      - Matches whale hold behavior (58bro/nervousdegen don't flip easily)

    A pivot-high at bar j requires highs[j] to be the unique max of the
    window [j-pivot_bars, j+pivot_bars]. Mirror for pivot-low. A pivot is
    CONFIRMED only once `pivot_bars` bars have played out on the right side
    (unavoidable delay — can't know a pivot until price has retreated).

    Returns (up_struct, dn_struct) aligned to df_5m timestamps via
    searchsorted, matching the shape of ``trend_lookup_1h``.
    """
    n = len(df_5m)
    if df_1h is None or df_1h.empty or len(df_1h) < 2 * pivot_bars + 2:
        up = np.ones(n, dtype=bool); dn = np.ones(n, dtype=bool)
        if return_bos:
            return up, dn, np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
        return up, dn

    highs = df_1h['high'].to_numpy(dtype=float)
    lows = df_1h['low'].to_numpy(dtype=float)
    closes = df_1h['close'].to_numpy(dtype=float)
    # ATR on the 1h timeframe — drives the displacement thresholds.
    # Missing column / NaNs → fall back to zero displacement (no filter).
    if 'atr' in df_1h.columns:
        atr_1h = df_1h['atr'].to_numpy(dtype=float)
        atr_1h = np.where(np.isnan(atr_1h), 0.0, atr_1h)
    else:
        atr_1h = np.zeros(len(df_1h), dtype=float)
    n_1h = len(df_1h)

    up_1h = np.zeros(n_1h, dtype=bool)
    dn_1h = np.zeros(n_1h, dtype=bool)
    # BOS-fresh flags: True on the 1h bar where protected_high (up) or
    # protected_low (down) just advanced in the trend direction. Fires once
    # per continuation event, so callers can gate pyramid adds on it.
    bos_up_1h = np.zeros(n_1h, dtype=bool)
    bos_dn_1h = np.zeros(n_1h, dtype=bool)

    state = "unknown"
    protected_high = None  # the swing high that defines the uptrend
    protected_low = None   # the swing low that defines the uptrend/downtrend
    # History of recent confirmed pivots — needed to set the new-trend
    # protected level after a CHoCH flip.
    recent_pivot_highs = []   # list of (bar_idx, value)
    recent_pivot_lows = []
    # Hardening state: pending CHoCH (wait for confirmation_bars more) and
    # cooldown tracker (min_hold_bars between flips).
    pending_flip = None           # None | "up" | "down" — direction of pending flip
    pending_since = -1            # bar idx when pending was set
    bars_since_flip = 10_000      # steps since last state change (starts large so no initial cooldown)

    for i in range(n_1h):
        # Confirm the pivot candidate at j = i - pivot_bars (needs pivot_bars
        # on each side).
        j = i - pivot_bars
        new_pivot_high = None
        new_pivot_low = None
        if j - pivot_bars >= 0 and j + pivot_bars < n_1h:
            lw = j - pivot_bars
            rw = j + pivot_bars + 1
            window_hi = highs[lw:rw]
            window_lo = lows[lw:rw]
            centre_hi = highs[j]
            centre_lo = lows[j]
            # Strictly-unique max / min (ties don't qualify as pivots)
            if centre_hi == window_hi.max() and np.sum(window_hi == centre_hi) == 1:
                new_pivot_high = centre_hi
                recent_pivot_highs.append((j, centre_hi))
                if len(recent_pivot_highs) > 10:
                    recent_pivot_highs.pop(0)
            if centre_lo == window_lo.min() and np.sum(window_lo == centre_lo) == 1:
                new_pivot_low = centre_lo
                recent_pivot_lows.append((j, centre_lo))
                if len(recent_pivot_lows) > 10:
                    recent_pivot_lows.pop(0)

        # --- State transitions ---
        if state == "unknown":
            # Establish initial trend: need at least one confirmed pivot high
            # AND low. First pivot pair sets the protected levels; state
            # begins as the direction of the more recent confirmation.
            if new_pivot_high is not None and protected_low is not None:
                state = "up"
                protected_high = new_pivot_high
            elif new_pivot_low is not None and protected_high is not None:
                state = "down"
                protected_low = new_pivot_low
            else:
                if new_pivot_high is not None:
                    protected_high = new_pivot_high
                if new_pivot_low is not None:
                    protected_low = new_pivot_low

        elif state == "up":
            atr_i = atr_1h[i]
            price_i = closes[i]
            # Displacement with absolute % floor: max(ATR-based, pct-based).
            # On low-ATR assets the pct floor binds; on liquid assets ATR binds.
            bos_min_gap = max(atr_i * min_bos_displacement_atr,
                              price_i * min_bos_displacement_pct)
            choch_min_gap = max(atr_i * min_choch_displacement_atr,
                                price_i * min_choch_displacement_pct)

            # BOS continuation — raise protected high if new pivot clears gap
            if (new_pivot_high is not None
                    and protected_high is not None
                    and new_pivot_high > protected_high + bos_min_gap):
                protected_high = new_pivot_high
                bos_up_1h[i] = True
            elif (new_pivot_high is not None and protected_high is None):
                protected_high = new_pivot_high
            # Update protected low on fresh pivot low
            if new_pivot_low is not None and (protected_low is None or new_pivot_low > protected_low):
                protected_low = new_pivot_low

            # ---- CHoCH with cooldown + 2-bar confirmation ----
            cooldown_ok = bars_since_flip >= min_hold_bars
            break_dn = (protected_low is not None
                        and closes[i] < protected_low - choch_min_gap)
            if cooldown_ok:
                if pending_flip == "down":
                    # Confirmation bar — did break hold?
                    if break_dn:
                        state = "down"
                        if recent_pivot_highs:
                            protected_high = recent_pivot_highs[-1][1]
                        bars_since_flip = 0
                        pending_flip = None
                    else:
                        pending_flip = None  # failed confirmation, discard
                elif break_dn:
                    # Arm pending flip — will need next bar to confirm
                    pending_flip = "down"
                    pending_since = i
                elif pending_flip == "up":
                    # Stale pending in wrong direction — clear
                    pending_flip = None
            else:
                pending_flip = None  # cooldown blocks any pending

        elif state == "down":
            atr_i = atr_1h[i]
            price_i = closes[i]
            bos_min_gap = max(atr_i * min_bos_displacement_atr,
                              price_i * min_bos_displacement_pct)
            choch_min_gap = max(atr_i * min_choch_displacement_atr,
                                price_i * min_choch_displacement_pct)

            if (new_pivot_low is not None
                    and protected_low is not None
                    and new_pivot_low < protected_low - bos_min_gap):
                protected_low = new_pivot_low
                bos_dn_1h[i] = True
            elif (new_pivot_low is not None and protected_low is None):
                protected_low = new_pivot_low
            if new_pivot_high is not None and (protected_high is None or new_pivot_high < protected_high):
                protected_high = new_pivot_high

            cooldown_ok = bars_since_flip >= min_hold_bars
            break_up = (protected_high is not None
                        and closes[i] > protected_high + choch_min_gap)
            if cooldown_ok:
                if pending_flip == "up":
                    if break_up:
                        state = "up"
                        if recent_pivot_lows:
                            protected_low = recent_pivot_lows[-1][1]
                        bars_since_flip = 0
                        pending_flip = None
                    else:
                        pending_flip = None
                elif break_up:
                    pending_flip = "up"
                    pending_since = i
                elif pending_flip == "down":
                    pending_flip = None
            else:
                pending_flip = None

        # Advance cooldown counter each bar
        bars_since_flip += 1

        up_1h[i] = (state == "up")
        dn_1h[i] = (state == "down")

    # Align to 5m timestamps — BIAS FIX: use last CLOSED 1h bar to avoid
    # look-ahead (see note in hma_slope_lookup_1h).
    ts_5m = df_5m['timestamp'].to_numpy()
    ts_1h = df_1h['timestamp'].to_numpy()
    raw = np.searchsorted(ts_1h, ts_5m, side='right')
    idx = np.clip(raw - 2, 0, n_1h - 1)
    before = raw <= 1
    up = up_1h[idx]
    dn = dn_1h[idx]
    up = np.where(before, True, up)
    dn = np.where(before, True, dn)
    if return_bos:
        # BOS flags: only True on 5m bars that fall on the FIRST mapping of
        # each 1h BOS event — otherwise we'd report "fresh BOS" on every 5m
        # bar for an hour. Emit only on the transition.
        bos_up = bos_up_1h[idx]
        bos_dn = bos_dn_1h[idx]
        # Keep True only on the 5m bar where idx changes (i.e. crosses into
        # a new 1h bucket). Everything else False.
        first_of_hour = np.zeros(n, dtype=bool)
        first_of_hour[0] = True
        first_of_hour[1:] = idx[1:] != idx[:-1]
        bos_up = bos_up & first_of_hour
        bos_dn = bos_dn & first_of_hour
        return up, dn, bos_up, bos_dn
    return up, dn


def last_pivot_levels_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                                  lookback: int = 3):
    """Per-5m-bar array of the most recent CONFIRMED 1h pivot high and low.

    A 1h pivot at index k is "confirmed" at ts_1h[k + lookback] (need lookback
    bars on each side). For each 5m bar at time t, return the last pivot whose
    confirmation_time <= t. NaN before the first confirmation. Used by BOS
    strategies to know the levels that a break would violate.
    """
    n = len(df_5m)
    last_ph = np.full(n, np.nan)
    last_pl = np.full(n, np.nan)
    if df_1h is None or df_1h.empty or len(df_1h) < 2 * lookback + 1:
        return last_ph, last_pl
    highs = df_1h['high'].to_numpy()
    lows = df_1h['low'].to_numpy()
    ts_1h = df_1h['timestamp'].to_numpy()
    ts_5m = df_5m['timestamp'].to_numpy()

    conf_h = []
    conf_l = []
    for k in range(lookback, len(df_1h) - lookback):
        wh = highs[k - lookback: k + lookback + 1]
        wl = lows[k - lookback: k + lookback + 1]
        if highs[k] == wh.max() and (wh == highs[k]).sum() == 1:
            conf_h.append((ts_1h[k + lookback], float(highs[k])))
        if lows[k] == wl.min() and (wl == lows[k]).sum() == 1:
            conf_l.append((ts_1h[k + lookback], float(lows[k])))

    ih = il = 0
    cur_h = np.nan; cur_l = np.nan
    for i in range(n):
        t = ts_5m[i]
        while ih < len(conf_h) and conf_h[ih][0] <= t:
            cur_h = conf_h[ih][1]; ih += 1
        while il < len(conf_l) and conf_l[il][0] <= t:
            cur_l = conf_l[il][1]; il += 1
        last_ph[i] = cur_h
        last_pl[i] = cur_l
    return last_ph, last_pl


def add_funding_features(df_5m: pd.DataFrame, funding_df: pd.DataFrame,
                         z_window_hours: int = 24) -> pd.DataFrame:
    """Join hourly funding-rate history onto a 5m-bar DataFrame.

    funding_df columns: [timestamp, funding_rate, premium] (one row per hour).
    Returns a new DataFrame with these added columns (NaN where funding_df
    doesn't cover the 5m bar — happens at the very front of history):

        funding_rate    — hourly funding at the 5m bar's hour
        funding_z24h    — z-score of funding vs prior z_window_hours
        funding_cum24h  — rolling sum of last z_window_hours of hourly funding
        premium         — mark-vs-oracle premium at the 5m bar's hour
        funding_extreme — +1 crowded-longs, -1 crowded-shorts, 0 otherwise
                          (threshold: |funding_z24h| >= 2.0)

    Contrarian hypothesis: funding_extreme=+1 → future DOWN move likely
    (crowded longs get flushed). Hit rate tested in scripts/score_funding.py.
    """
    df = df_5m.copy()
    if funding_df is None or funding_df.empty:
        df["funding_rate"] = np.nan
        df["funding_z24h"] = np.nan
        df["funding_cum24h"] = np.nan
        df["premium"] = np.nan
        df["funding_extreme"] = 0
        return df

    f = funding_df.sort_values("timestamp").reset_index(drop=True).copy()
    rolling_mean = f["funding_rate"].rolling(z_window_hours, min_periods=4).mean()
    rolling_std = f["funding_rate"].rolling(z_window_hours, min_periods=4).std()
    f["funding_z"] = (f["funding_rate"] - rolling_mean) / rolling_std.replace(0, np.nan)
    f["funding_cum"] = f["funding_rate"].rolling(z_window_hours, min_periods=1).sum()

    ts_5m = df["timestamp"].to_numpy()
    ts_1h = f["timestamp"].to_numpy()
    idx = np.searchsorted(ts_1h, ts_5m, side="right") - 1
    before_first = idx < 0
    idx_clipped = np.clip(idx, 0, len(f) - 1)

    df["funding_rate"] = np.where(before_first, np.nan,
                                  f["funding_rate"].to_numpy()[idx_clipped])
    df["funding_z24h"] = np.where(before_first, np.nan,
                                  f["funding_z"].to_numpy()[idx_clipped])
    df["funding_cum24h"] = np.where(before_first, np.nan,
                                    f["funding_cum"].to_numpy()[idx_clipped])
    df["premium"] = np.where(before_first, np.nan,
                             f["premium"].to_numpy()[idx_clipped])

    extreme = np.zeros(len(df), dtype=int)
    z = df["funding_z24h"].to_numpy()
    extreme[np.nan_to_num(z, nan=0.0) >= 2.0] = 1
    extreme[np.nan_to_num(z, nan=0.0) <= -2.0] = -1
    df["funding_extreme"] = extreme
    return df


def _kalman_velocity(y: np.ndarray, q_level: float = 1e-5,
                     q_vel: float = 1e-7, r: float = 1e-3) -> tuple:
    """2-state (level + velocity) Kalman filter on a 1D series.

    State:  x = [level, velocity]^T
    Transition: level_{t+1} = level_t + velocity_t;  velocity_{t+1} = velocity_t
    Observation: y_t = level_t + noise

    q_level, q_vel = process noise (how much level/velocity can drift per step)
    r             = observation noise (how noisy y is)

    Returns (level_est, velocity_est) — two arrays same length as y.
    """
    n = len(y)
    level = np.full(n, np.nan)
    velocity = np.full(n, np.nan)
    if n == 0:
        return level, velocity

    F = np.array([[1.0, 1.0], [0.0, 1.0]])     # transition
    H = np.array([[1.0, 0.0]])                  # observation: only level
    Q = np.array([[q_level, 0.0], [0.0, q_vel]])
    R = np.array([[r]])

    x = np.array([[y[0]], [0.0]])                         # state
    P = np.array([[1.0, 0.0], [0.0, 1.0]])                # covariance
    level[0] = x[0, 0]
    velocity[0] = x[1, 0]

    for t in range(1, n):
        x = F @ x                                          # predict
        P = F @ P @ F.T + Q
        if not np.isnan(y[t]):
            z = np.array([[y[t]]])
            S = H @ P @ H.T + R
            K = P @ H.T @ np.linalg.inv(S)                 # Kalman gain
            x = x + K @ (z - H @ x)                        # update
            P = (np.eye(2) - K @ H) @ P
        level[t] = x[0, 0]
        velocity[t] = x[1, 0]
    return level, velocity


def kalman_slope_lookup_1h(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                           min_velocity_pct: float = 0.0005) -> tuple:
    """Kalman-filtered velocity regime on the 1h timeframe.

    Fits a level+velocity Kalman filter on 1h log-close. Emits UP when the
    filter's velocity state > min_velocity_pct × price, DOWN when <
    -min_velocity_pct × price, neither otherwise. Unlike HMA (which needs
    full history to recompute), Kalman updates adaptively one bar at a time.

    Process/obs noise params chosen from rough calibration — tighter smoothing
    than HMA(14), less lag than EMA(50). Can be swept in OOS if needed.

    Returns (up, dn) boolean arrays aligned to df_5m timestamps.
    """
    n = len(df_5m)
    if df_1h is None or df_1h.empty or len(df_1h) < 10:
        return np.ones(n, dtype=bool), np.ones(n, dtype=bool)

    closes = df_1h["close"].to_numpy(dtype=float)
    log_close = np.log(closes)
    level, velocity = _kalman_velocity(log_close, q_level=1e-5, q_vel=1e-7, r=1e-4)
    # Convert log-return velocity to price-return percentage threshold
    threshold = min_velocity_pct  # velocity is already in log-return space
    up_1h = np.zeros(len(df_1h), dtype=bool)
    dn_1h = np.zeros(len(df_1h), dtype=bool)
    valid = ~np.isnan(velocity)
    up_1h[valid] = velocity[valid] > threshold
    dn_1h[valid] = velocity[valid] < -threshold

    return _align_1h_to_5m(up_1h, dn_1h, df_5m, df_1h)
