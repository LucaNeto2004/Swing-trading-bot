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
    idx = np.searchsorted(ts_1h, ts_5m, side='right') - 1
    idx = np.clip(idx, 0, len(ts_1h) - 1)
    up = e21[idx] > e50[idx]
    dn = e21[idx] < e50[idx]
    before = np.searchsorted(ts_1h, ts_5m, side='right') == 0
    up = np.where(before, True, up)
    dn = np.where(before, True, dn)
    return up, dn
