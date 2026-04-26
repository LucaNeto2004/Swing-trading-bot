"""Grid-search whale_swing configs on HL xyz commodity perps.

Symbols: xyz:GOLD, xyz:SILVER, xyz:CL (WTI), xyz:BRENTOIL.
Timeframe: 15m entries, 1h / 4h trend filters (same as crypto backtest).
Trading hours: weekdays only (Mon–Fri). Finer hour gates can be added later.
Commission: 0.030% per side (tier 0 conservative; matches current bot assumption).

Output: for each symbol, the top configs ranked by profit factor (PF ≥ 1.3,
trades ≥ 20, positive P&L). Writes a JSON summary to /tmp/commod_backtest.json.
"""
import os
import sys
import json
import time
from dataclasses import dataclass
from itertools import product

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd
import requests

from core.features import trend_lookup_1h, structure_lookup_1h, hma_slope_lookup_1h, sjm_lookup_1h, kalman_slope_lookup_1h
from core.quant_filters import (
    rolling_hurst, compute_adx, validated_pivots_1h, combined_pivots_1h,
    align_1h_to_entry,
)

HL_API = "https://api.hyperliquid.xyz/info"
COMMISSION = 0.00030
CACHE = "/tmp/commod_backtest_cache"
os.makedirs(CACHE, exist_ok=True)

# Time-stop — mirrors config/settings.py::TimeStopConfig defaults. Motivated
# by HL trader 0x1aa780bb… (2026-04-21 study): cut stale trades after ~4h if
# they haven't moved in favor. Backtester runs on 15m candles so 16 bars = 4h
# here; live bot runs on 5m so config/settings.py uses stale_bars=48 (=4h).
# Set TIME_STOP_ENABLED=False to A/B compare.
TIME_STOP_ENABLED = True
TIME_STOP_STALE_BARS = 16         # 4h on 15m
TIME_STOP_MIN_MFE_ATR = 0.3

# INVERT mode — flips every entry's side before sizing the position. All
# filters / RSI triggers / structure gates still fire as normal; we just
# swap long↔short at the moment of opening. SL/TP distances are the same
# ATR multiples, just on the opposite side. Used for the 2026-04-22
# "what if we did the opposite?" experiment.
INVERT_SIDE = False

SYMBOLS = ["xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL"]
# Leverage caps on xyz HIP-3 — conservative 5× for commodities (vs 10-40 crypto).
# Effective sizing = margin_pct × leverage. We'll try margin=0.15 × lev=5 = 0.75× notional.
LEV_CAP = {"xyz:GOLD": 5, "xyz:SILVER": 5, "xyz:CL": 5, "xyz:BRENTOIL": 5}


def fetch_hl(symbol: str, interval: str, bars_target: int = 4000) -> pd.DataFrame:
    cache = os.path.join(CACHE, f"{symbol.replace(':','_')}_{interval}.csv")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3600 * 4:
        return pd.read_csv(cache, parse_dates=["timestamp"])
    ms = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[interval]
    end = int(time.time() * 1000)
    all_data = []
    remaining = bars_target
    CHUNK = 4500
    while remaining > 0:
        take = min(CHUNK, remaining)
        start = end - ms * take
        r = requests.post(HL_API, json={"type": "candleSnapshot",
            "req": {"coin": symbol, "interval": interval, "startTime": start, "endTime": end}}, timeout=30)
        data = r.json() or []
        if not data:
            break
        all_data = data + all_data
        end = int(data[0]["t"]) - ms
        remaining -= len(data)
        if len(data) < take * 0.5:
            break
        time.sleep(0.3)
    seen = {int(c["t"]): c for c in all_data}
    rows = sorted(seen.values(), key=lambda c: int(c["t"]))
    df = pd.DataFrame([{
        "timestamp": pd.to_datetime(int(c["t"]), unit="ms", utc=True),
        "open": float(c["o"]), "high": float(c["h"]),
        "low": float(c["l"]), "close": float(c["c"]),
        "volume": float(c["v"]),
    } for c in rows])
    df.to_csv(cache, index=False)
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]
    df["ema_21"] = c.ewm(span=21, adjust=False).mean()
    df["ema_50"] = c.ewm(span=50, adjust=False).mean()
    df["ema_200"] = c.ewm(span=200, adjust=False).mean()
    df["ema_50_slope"] = (df["ema_50"] - df["ema_50"].shift(20)) / df["ema_50"].shift(20)
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    df["bb_mid"] = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    return df


@dataclass
class Cfg:
    trend_filter: str
    entry_type: str
    rsi_oversold: float
    rsi_overbought: float
    sl_atr: float
    tp1_atr: float
    tp1_pct: float
    tp2_atr: float
    tp2_pct: float
    tp3_atr: float
    tp3_pct: float
    trail_atr: float
    max_hold_bars: int
    direction: str
    use_1h_filter: bool
    trend_filter_1h: str
    require_4h_agreement: bool
    # exit_type: "standard" (SL + TP ladder + trail + max_hold — current) or
    # "bos_structural" (pure — no SL/TP, exit only when price closes past the
    #  opposing pivot level) or "bos_hybrid" (TP1 partial locked, rest rides
    #  to structural exit).
    exit_type: str = "standard"
    # require_funding_confirm: if True, long entries require funding_extreme==-1
    # (crowded shorts) at entry bar; short entries require funding_extreme==+1
    # (crowded longs). If arr["funding_extreme"] is missing (all zeros), this
    # gate is disabled silently so it doesn't break non-funding-aware tests.
    require_funding_confirm: bool = False
    # ensemble_k: consensus threshold for entry_type="ensemble_regime".
    # Entry fires when the count of agreeing filters crosses up through K
    # (i.e. prev_count < K and cur_count >= K). 5 filters: ema_cross, structure,
    # hma_slope, sjm, kalman. K=3 moderate, K=4 strong, K=5 unanimous.
    ensemble_k: int = 4
    # require_bos_confirm: when True, ensemble_regime entries also require
    # price to have broken the most-recent confirmed 1h pivot in the trade
    # direction. Makes entries stricter — regime agrees AND structure broke.
    require_bos_confirm: bool = False
    # disaster_sl_atr: hard floor on bos_structural / bos_hybrid exits. When > 0,
    # an absolute SL at entry ± atr*disaster_sl_atr is enforced even in structural
    # modes — structural exit still fires normally, but a gap through the
    # disaster level triggers first. 0 = disabled.
    disaster_sl_atr: float = 0.0


def _last_pivot_levels_aligned(df_entry: pd.DataFrame, df_1h: pd.DataFrame,
                                lookback: int = 3) -> tuple:
    """Per-entry-bar arrays of the most recent CONFIRMED 1h pivot high and
    pivot low, strictly using past data (no look-ahead).

    A 1h pivot at index i is "confirmed" at timestamp ts_1h[i + lookback]
    (you need `lookback` bars on each side to know it's a local extremum).
    For each entry-bar at time t, we take the last pivot whose confirmation
    time is <= t.

    Returns (last_ph, last_pl) — same length as df_entry, NaN before first
    confirmation.
    """
    n = len(df_entry)
    last_ph = np.full(n, np.nan)
    last_pl = np.full(n, np.nan)
    if df_1h is None or df_1h.empty or len(df_1h) < 2 * lookback + 1:
        return last_ph, last_pl
    highs_1h = df_1h["high"].to_numpy()
    lows_1h = df_1h["low"].to_numpy()
    ts_1h = df_1h["timestamp"].to_numpy()
    ts_entry = df_entry["timestamp"].to_numpy()

    conf_h = []  # (confirmation_ts, pivot_high_price)
    conf_l = []  # (confirmation_ts, pivot_low_price)
    for k in range(lookback, len(df_1h) - lookback):
        wh = highs_1h[k - lookback: k + lookback + 1]
        wl = lows_1h[k - lookback: k + lookback + 1]
        if highs_1h[k] == wh.max() and (wh == highs_1h[k]).sum() == 1:
            conf_h.append((ts_1h[k + lookback], float(highs_1h[k])))
        if lows_1h[k] == wl.min() and (wl == lows_1h[k]).sum() == 1:
            conf_l.append((ts_1h[k + lookback], float(lows_1h[k])))

    # Walk entry bars, maintain "most recent confirmed pivot" cursor
    ih = il = 0
    cur_h = np.nan; cur_l = np.nan
    for i in range(n):
        t = ts_entry[i]
        while ih < len(conf_h) and conf_h[ih][0] <= t:
            cur_h = conf_h[ih][1]; ih += 1
        while il < len(conf_l) and conf_l[il][0] <= t:
            cur_l = conf_l[il][1]; il += 1
        last_ph[i] = cur_h
        last_pl[i] = cur_l
    return last_ph, last_pl


def precompute(df15, df1h, df4h):
    a = {col: df15[col].to_numpy() for col in
         ["open","high","low","close","atr","rsi","ema_21","ema_50","ema_200",
          "ema_50_slope","bb_lower","bb_upper"]}
    up_e, dn_e = trend_lookup_1h(df15, df1h)
    up_s, dn_s = structure_lookup_1h(df15, df1h)
    up_4h, dn_4h = structure_lookup_1h(df15, df4h, pivot_bars=3)
    up_h, dn_h = hma_slope_lookup_1h(df15, df1h)
    up_j, dn_j = sjm_lookup_1h(df15, df1h)
    up_k, dn_k = kalman_slope_lookup_1h(df15, df1h)
    last_ph, last_pl = _last_pivot_levels_aligned(df15, df1h, lookback=3)
    # Quant regime filters — Hurst + ADX on 1h, aligned causally to entry TF.
    # Use int64 ns timestamps to avoid tz-aware / naive Timestamp issues.
    ts_1h = pd.to_datetime(df1h["timestamp"], utc=True).astype("int64").to_numpy()
    ts_entry = pd.to_datetime(df15["timestamp"], utc=True).astype("int64").to_numpy()
    close_1h = df1h["close"].to_numpy()
    hurst_1h = rolling_hurst(close_1h, window=100)
    adx_1h = compute_adx(df1h["high"].to_numpy(), df1h["low"].to_numpy(),
                         close_1h, period=14)
    hurst_entry = align_1h_to_entry(hurst_1h, ts_1h, ts_entry)
    adx_entry = align_1h_to_entry(adx_1h, ts_1h, ts_entry)
    # Validated pivots — each is a discrete event at confirmation time. Build
    # boolean arrays aligned to entry-TF bars: valid_piv_h[t] = True iff a
    # validated pivot_H confirmed on 1h at or before timestamp t AND within
    # the last 2 5m bars (i.e. a "fresh" event — not a stale signal).
    # Combined fractal + HMA-smoothed peaks with 1-validator + ATR-move gate.
    # Smoothed peaks catch turning points even in noisy bars where raw
    # fractals trigger too late. Dedupped by bar.
    valid_h, valid_l = combined_pivots_1h(df1h, df_4h=df4h,
                                           fractal_lookback=3,
                                           smoothed_lookback=2,
                                           atr_min_move=1.0,
                                           vol_spike=1.3,
                                           rsi_extreme=(35.0, 65.0))
    pivot_h_event = np.zeros(len(df15), dtype=bool)
    pivot_l_event = np.zeros(len(df15), dtype=bool)
    # Also record the pivot LEVEL at confirmation — for exit targeting.
    pivot_h_level = np.full(len(df15), np.nan)
    pivot_l_level = np.full(len(df15), np.nan)
    # For each pivot, find the first 5m bar where ts_entry[i] >= confirm_ts
    # and mark a 1-bar "event" there. Subsequent bars stay False until a new
    # pivot confirms — so the entry bar is precisely the detection bar.
    for confirm_ts, _, level, _ in valid_h:
        i = int(np.searchsorted(ts_entry, confirm_ts, side="left"))
        if i < len(df15):
            pivot_h_event[i] = True
            pivot_h_level[i:] = level  # forward-fill for exit logic
    for confirm_ts, _, level, _ in valid_l:
        i = int(np.searchsorted(ts_entry, confirm_ts, side="left"))
        if i < len(df15):
            pivot_l_event[i] = True
            pivot_l_level[i:] = level

    a.update({"up_1h": up_e, "dn_1h": dn_e,
              "up_struct": up_s, "dn_struct": dn_s,
              "up_hma": up_h, "dn_hma": dn_h,
              "up_sjm": up_j, "dn_sjm": dn_j,
              "up_kalman": up_k, "dn_kalman": dn_k,
              "up_4h": up_4h, "dn_4h": dn_4h,
              "last_pivot_h": last_ph, "last_pivot_l": last_pl,
              "hurst": hurst_entry, "adx": adx_entry,
              "valid_piv_h": pivot_h_event, "valid_piv_l": pivot_l_event,
              "valid_piv_h_level": pivot_h_level,
              "valid_piv_l_level": pivot_l_level,
              "timestamp": ts_entry})
    # weekday mask (Mon=0 ... Sun=6); commodities closed on weekends
    dow = pd.DatetimeIndex(df15["timestamp"]).dayofweek.to_numpy()
    a["weekday"] = dow < 5
    return a


def backtest(arr, cfg: Cfg, leverage: float, i_start: int = 52):
    n = len(arr["close"])
    close, high, low = arr["close"], arr["high"], arr["low"]
    rsi, atr = arr["rsi"], arr["atr"]
    e21, e50, e200 = arr["ema_21"], arr["ema_50"], arr["ema_200"]
    slope = arr["ema_50_slope"]
    bbl, bbu = arr["bb_lower"], arr["bb_upper"]
    ts = arr["timestamp"]
    weekday = arr["weekday"]

    if cfg.trend_filter_1h == "structure":
        up_1h, dn_1h = arr["up_struct"], arr["dn_struct"]
    elif cfg.trend_filter_1h == "both_agree":
        up_1h = arr["up_1h"] & arr["up_struct"]
        dn_1h = arr["dn_1h"] & arr["dn_struct"]
    elif cfg.trend_filter_1h == "hma_slope":
        up_1h, dn_1h = arr["up_hma"], arr["dn_hma"]
    elif cfg.trend_filter_1h == "sjm":
        up_1h, dn_1h = arr["up_sjm"], arr["dn_sjm"]
    elif cfg.trend_filter_1h == "kalman":
        up_1h, dn_1h = arr["up_kalman"], arr["dn_kalman"]
    else:
        up_1h, dn_1h = arr["up_1h"], arr["dn_1h"]

    trades = []
    position = None
    tp1_hit = tp2_hit = tp3_hit = False

    for i in range(max(i_start, 52), n):
        a_i = atr[i]; r = rsi[i]
        if a_i <= 0 or a_i != a_i or r != r:
            continue
        r_prev = rsi[i-1]; price = close[i]; hi = high[i]; lo = low[i]

        # Manage open position
        if position is not None:
            side = position["side"]; entry = position["entry"]; trl = position["trail_offset"]
            if trl > 0:
                if side == "long":
                    if hi > position["best"]: position["best"] = hi
                    if not position["trail_active"] and hi >= entry + trl:
                        position["trail_active"] = True
                    if position["trail_active"]:
                        ns = position["best"] - trl
                        if ns > position["sl"]: position["sl"] = ns
                else:
                    if lo < position["best"]: position["best"] = lo
                    if not position["trail_active"] and lo <= entry - trl:
                        position["trail_active"] = True
                    if position["trail_active"]:
                        ns = position["best"] + trl
                        if ns < position["sl"]: position["sl"] = ns

            if not tp1_hit and position["tp1"] is not None:
                tp1 = position["tp1"]
                if (side == "long" and hi >= tp1) or (side == "short" and lo <= tp1):
                    tp1_hit = True; pct = cfg.tp1_pct
                    pnl_p = ((tp1 - entry) if side == "long" else (entry - tp1)) * position["size"] * pct
                    pnl_p -= position["notional"] * pct * COMMISSION
                    trades.append({"pnl": pnl_p, "reason": "tp1", "ts": ts[i],
                                   "entry_bar": position["entry_bar"], "side": side})
                    position["size"] *= (1 - pct); position["notional"] *= (1 - pct)
                    position["sl"] = entry
            if tp1_hit and not tp2_hit and position.get("tp2") is not None:
                tp2 = position["tp2"]
                if (side == "long" and hi >= tp2) or (side == "short" and lo <= tp2):
                    tp2_hit = True; pct = cfg.tp2_pct
                    pnl_p = ((tp2 - entry) if side == "long" else (entry - tp2)) * position["size"] * pct
                    pnl_p -= position["notional"] * pct * COMMISSION
                    trades.append({"pnl": pnl_p, "reason": "tp2", "ts": ts[i],
                                   "entry_bar": position["entry_bar"], "side": side})
                    position["size"] *= (1 - pct); position["notional"] *= (1 - pct)
            if tp2_hit and not tp3_hit and position.get("tp3") is not None:
                tp3 = position["tp3"]
                if (side == "long" and hi >= tp3) or (side == "short" and lo <= tp3):
                    tp3_hit = True; pct = cfg.tp3_pct
                    pnl_p = ((tp3 - entry) if side == "long" else (entry - tp3)) * position["size"] * pct
                    pnl_p -= position["notional"] * pct * COMMISSION
                    trades.append({"pnl": pnl_p, "reason": "tp3", "ts": ts[i],
                                   "entry_bar": position["entry_bar"], "side": side})
                    position["size"] *= (1 - pct); position["notional"] *= (1 - pct)

            # Track max favorable excursion (ATR multiples) for time-stop + analytics
            if position["entry_atr"] > 0:
                fav = (hi - entry) if side == "long" else (entry - lo)
                fav_atr = fav / position["entry_atr"]
                if fav_atr > position["max_fav"]:
                    position["max_fav"] = fav_atr

            # BOS structural exit — close when price crosses opposing pivot.
            # Overrides the normal SL when exit_type == bos_structural / bos_hybrid.
            bos_exit_hit = False
            regime_exit_hit = False
            disaster_hit = False
            if cfg.exit_type in ("bos_structural", "bos_hybrid"):
                # Disaster SL — hard floor that fires on wide gaps through the
                # opposing pivot before structural exit can catch up.
                d = position.get("disaster_sl", 0.0) or 0.0
                if d > 0:
                    if side == "long" and lo <= d:
                        disaster_hit = True
                        position["sl"] = d
                    elif side == "short" and hi >= d:
                        disaster_hit = True
                        position["sl"] = d
                opp_pivot = arr["last_pivot_l"][i] if side == "long" else arr["last_pivot_h"][i]
                if not disaster_hit and not np.isnan(opp_pivot):
                    if side == "long" and close[i] < opp_pivot:
                        bos_exit_hit = True
                        position["sl"] = opp_pivot
                    elif side == "short" and close[i] > opp_pivot:
                        bos_exit_hit = True
                        position["sl"] = opp_pivot
                sl_hit = False
            elif cfg.exit_type in ("regime_flip", "regime_flip_hybrid"):
                # Exit when the filter stops agreeing with the trade's direction.
                if side == "long" and not up_1h[i]:
                    regime_exit_hit = True
                elif side == "short" and not dn_1h[i]:
                    regime_exit_hit = True
                sl_hit = False
            elif cfg.exit_type == "pullback_exit":
                # Exit when next opposite-side validated pivot confirms OR
                # regime turns against us. 3% SL is enforced via cfg.sl_atr
                # (set externally by the backtest config). Max hold is standard.
                regime_exit_hit = False
                if side == "long" and arr["valid_piv_h"][i]:
                    regime_exit_hit = True
                elif side == "short" and arr["valid_piv_l"][i]:
                    regime_exit_hit = True
                else:
                    # Regime flip-check: if regime becomes hostile to the trade
                    up_cnt = int(arr["up_1h"][i]) + int(arr["up_struct"][i]) + \
                             int(arr["up_hma"][i]) + int(arr["up_sjm"][i]) + int(arr["up_kalman"][i])
                    dn_cnt = int(arr["dn_1h"][i]) + int(arr["dn_struct"][i]) + \
                             int(arr["dn_hma"][i]) + int(arr["dn_sjm"][i]) + int(arr["dn_kalman"][i])
                    vote = up_cnt - dn_cnt
                    if side == "long" and vote <= -2:
                        regime_exit_hit = True   # flipped red against us
                    elif side == "short" and vote >= 2:
                        regime_exit_hit = True   # flipped green against us
                # SL active (3% flat — see cfg.sl_atr handling at entry)
                sl_hit = (lo <= position["sl"]) if side == "long" else (hi >= position["sl"])
            elif cfg.exit_type in ("ensemble_regime", "ensemble_hybrid"):
                # Exit when the ensemble consensus for the trade direction
                # drops below (K - 1). Symmetric lenient-exit threshold.
                up_cnt = int(arr["up_1h"][i]) + int(arr["up_struct"][i]) + \
                         int(arr["up_hma"][i]) + int(arr["up_sjm"][i]) + int(arr["up_kalman"][i])
                dn_cnt = int(arr["dn_1h"][i]) + int(arr["dn_struct"][i]) + \
                         int(arr["dn_hma"][i]) + int(arr["dn_sjm"][i]) + int(arr["dn_kalman"][i])
                if "up_pelt" in arr and "dn_pelt" in arr:
                    up_cnt += int(arr["up_pelt"][i]); dn_cnt += int(arr["dn_pelt"][i])
                k_exit = max(cfg.ensemble_k - 1, 1)
                if side == "long" and up_cnt < k_exit:
                    regime_exit_hit = True
                elif side == "short" and dn_cnt < k_exit:
                    regime_exit_hit = True
                sl_hit = False
            else:
                sl_hit = (lo <= position["sl"]) if side == "long" else (hi >= position["sl"])
            bars_in = i - position["entry_bar"]
            max_hold_hit = bars_in >= cfg.max_hold_bars
            time_stop_hit = (
                TIME_STOP_ENABLED
                and bars_in >= TIME_STOP_STALE_BARS
                and position["max_fav"] < TIME_STOP_MIN_MFE_ATR
                and not tp1_hit
            )
            if sl_hit or bos_exit_hit or regime_exit_hit or disaster_hit or max_hold_hit or time_stop_hit:
                ep = position["sl"] if (sl_hit or bos_exit_hit or disaster_hit) else price
                pnl = ((ep - entry) if side == "long" else (entry - ep)) * position["size"]
                pnl -= position["notional"] * COMMISSION
                if disaster_hit:
                    reason = "disaster_sl"
                elif bos_exit_hit:
                    reason = "bos_exit"
                elif regime_exit_hit:
                    reason = "regime_exit"
                elif position["trail_active"] and sl_hit:
                    reason = "trail_stop"
                elif sl_hit:
                    reason = "sl"
                elif time_stop_hit:
                    reason = "time_stop"
                else:
                    reason = "max_hold"
                trades.append({"pnl": pnl, "reason": reason, "ts": ts[i],
                               "entry_bar": position["entry_bar"], "side": side})
                position = None
                tp1_hit = tp2_hit = tp3_hit = False
                continue

        if position is not None:
            continue

        # Gate: weekday only
        if not weekday[i]:
            continue

        # 5m trend filter (applied on 15m here — we use generic "trend_filter")
        tf = cfg.trend_filter
        if tf == "ema_cross":
            up_ok, dn_ok = e21[i] > e50[i], e21[i] < e50[i]
        elif tf == "ema_slope":
            up_ok, dn_ok = slope[i] > 0, slope[i] < 0
        elif tf == "ema200":
            up_ok = price > e200[i] if e200[i] > 0 else True
            dn_ok = price < e200[i] if e200[i] > 0 else True
        else:
            up_ok, dn_ok = True, True

        if cfg.use_1h_filter:
            if not up_1h[i]: up_ok = False
            if not dn_1h[i]: dn_ok = False
        if cfg.require_4h_agreement:
            if not arr["up_4h"][i]: up_ok = False
            if not arr["dn_4h"][i]: dn_ok = False
        if cfg.direction == "long_only": dn_ok = False
        elif cfg.direction == "short_only": up_ok = False

        # Funding-extreme confirmation (quant contrarian signal).
        # Crowded shorts (extreme = -1) → expect squeeze up → allow longs
        # Crowded longs (extreme = +1) → expect flush down → allow shorts
        # Disabled silently if arr doesn't have funding data.
        if cfg.require_funding_confirm and "funding_extreme" in arr:
            fe = arr["funding_extreme"][i] if i < len(arr["funding_extreme"]) else 0
            if fe != -1: up_ok = False
            if fe != 1: dn_ok = False

        long_trig = short_trig = False
        et = cfg.entry_type
        if et == "rsi_bounce":
            long_trig = up_ok and r_prev < cfg.rsi_oversold and r >= cfg.rsi_oversold
            short_trig = dn_ok and r_prev > cfg.rsi_overbought and r <= cfg.rsi_overbought
        elif et == "bb_touch":
            long_trig = up_ok and low[i-1] <= bbl[i] and price > high[i-1]
            short_trig = dn_ok and high[i-1] >= bbu[i] and price < low[i-1]
        elif et == "ema_bounce":
            long_trig = up_ok and low[i-1] <= e21[i] and price > e21[i] and r_prev < 45
            short_trig = dn_ok and high[i-1] >= e21[i] and price < e21[i] and r_prev > 55
        elif et == "swing_pivot":
            if i >= 55:
                if up_ok and low[i-3] < low[i-4] and low[i-3] < low[i-2] and price > high[i-1]:
                    long_trig = True
                if dn_ok and high[i-3] > high[i-4] and high[i-3] > high[i-2] and price < low[i-1]:
                    short_trig = True
        elif et == "regime_flip":
            # Fresh-transition into UP / DN. Fires on the bar where the filter
            # flips regime state. Simplest possible signal-driven entry.
            if i >= 55:
                prev_up = up_1h[i - 1]
                prev_dn = dn_1h[i - 1]
                if up_ok and not prev_up:
                    long_trig = True
                if dn_ok and not prev_dn:
                    short_trig = True
        elif et == "bos_structural":
            # Pure break-of-structure. Fires when price closes above most-recent
            # confirmed 1h pivot H (in up regime) or below most-recent pivot L
            # (in down regime). No RSI / ATR gates — trust the filter + BOS.
            if i >= 55:
                ph = arr["last_pivot_h"][i]
                pl = arr["last_pivot_l"][i]
                if up_ok and not np.isnan(ph):
                    if close[i - 1] <= ph and price > ph:
                        long_trig = True
                if dn_ok and not np.isnan(pl):
                    if close[i - 1] >= pl and price < pl:
                        short_trig = True
        elif et == "pullback_in_regime":
            # Regime-aligned pivot pullback. Fires only when the regime
            # classifier agrees with the trade direction:
            #   trend_up + validated pivot_L → LONG
            #   trend_down + validated pivot_H → SHORT
            #   range + validated pivot_L → LONG
            #   range + validated pivot_H → SHORT
            # Chop → no trade. Impossible to short in a green regime.
            if i >= 55 and arr["valid_piv_l"][i] or (i >= 55 and arr["valid_piv_h"][i]):
                up_cnt = int(arr["up_1h"][i]) + int(arr["up_struct"][i]) + \
                         int(arr["up_hma"][i]) + int(arr["up_sjm"][i]) + int(arr["up_kalman"][i])
                dn_cnt = int(arr["dn_1h"][i]) + int(arr["dn_struct"][i]) + \
                         int(arr["dn_hma"][i]) + int(arr["dn_sjm"][i]) + int(arr["dn_kalman"][i])
                hurst = arr["hurst"][i]; adx_v = arr["adx"][i]
                vote = up_cnt - dn_cnt
                # Regime classification — looser thresholds for realistic
                # trade count on 41d crypto data. Trend: majority vote + Hurst
                # > 0.5 (persistent) OR ADX > 18 (either one qualifies).
                # Range: neutral vote + both Hurst < 0.5 and ADX < 25.
                if np.isnan(hurst) or np.isnan(adx_v):
                    hurst = 0.5; adx_v = 20.0  # neutral defaults for warm-up
                if vote >= 2 and (hurst > 0.5 or adx_v > 18):
                    regime = "trend_up"
                elif vote <= -2 and (hurst > 0.5 or adx_v > 18):
                    regime = "trend_down"
                elif abs(vote) <= 2 and hurst < 0.5 and adx_v < 25:
                    regime = "range"
                else:
                    regime = "chop"

                if arr["valid_piv_l"][i]:
                    if (regime == "trend_up" or regime == "range") and up_ok:
                        long_trig = True
                if arr["valid_piv_h"][i]:
                    if (regime == "trend_down" or regime == "range") and dn_ok:
                        short_trig = True
        elif et == "ensemble_regime":
            # Multi-filter consensus regime trading. Counts how many filters
            # (5 base: ema_cross, structure, hma_slope, sjm, kalman; optional
            # 6th: pelt) agree with each direction. Enters when consensus
            # crosses the threshold K upward (prev_count < K, cur_count >= K).
            # If require_bos_confirm, the BOS level must also be broken in
            # the trade direction.
            if i >= 55:
                up_cnt = int(arr["up_1h"][i]) + int(arr["up_struct"][i]) + \
                         int(arr["up_hma"][i]) + int(arr["up_sjm"][i]) + int(arr["up_kalman"][i])
                dn_cnt = int(arr["dn_1h"][i]) + int(arr["dn_struct"][i]) + \
                         int(arr["dn_hma"][i]) + int(arr["dn_sjm"][i]) + int(arr["dn_kalman"][i])
                up_cnt_p = int(arr["up_1h"][i-1]) + int(arr["up_struct"][i-1]) + \
                           int(arr["up_hma"][i-1]) + int(arr["up_sjm"][i-1]) + int(arr["up_kalman"][i-1])
                dn_cnt_p = int(arr["dn_1h"][i-1]) + int(arr["dn_struct"][i-1]) + \
                           int(arr["dn_hma"][i-1]) + int(arr["dn_sjm"][i-1]) + int(arr["dn_kalman"][i-1])
                if "up_pelt" in arr and "dn_pelt" in arr:
                    up_cnt   += int(arr["up_pelt"][i]);   dn_cnt   += int(arr["dn_pelt"][i])
                    up_cnt_p += int(arr["up_pelt"][i-1]); dn_cnt_p += int(arr["dn_pelt"][i-1])
                K = cfg.ensemble_k
                if up_ok and up_cnt >= K and up_cnt_p < K:
                    long_trig = True
                if dn_ok and dn_cnt >= K and dn_cnt_p < K:
                    short_trig = True
                if cfg.require_bos_confirm:
                    ph = arr["last_pivot_h"][i]; pl = arr["last_pivot_l"][i]
                    if long_trig and (np.isnan(ph) or price <= ph):
                        long_trig = False
                    if short_trig and (np.isnan(pl) or price >= pl):
                        short_trig = False
        elif et == "structural_breakout":
            # Break of confirmed 1h swing H/L with math gates to filter noise:
            #   - prev close was at/below the pivot (we're crossing it now)
            #   - current close is > pivot × 1.002 (real break, not just a nick)
            #   - break size >= 0.3 × ATR (scale-normalised minimum thrust)
            #   - RSI not in blow-off zone (avoid buying the climax)
            #   - ATR > 20-bar mean × 1.1 (real expansion, not sleeper bar)
            if i >= 55:
                ph = arr["last_pivot_h"][i]
                pl = arr["last_pivot_l"][i]
                atr_mean20 = np.nanmean(arr["atr"][max(0, i - 20): i]) if i > 0 else a_i
                atr_exp = atr_mean20 > 0 and a_i > atr_mean20 * 1.1
                if up_ok and not np.isnan(ph) and atr_exp:
                    prev_close_le = close[i - 1] <= ph
                    real_break = price > ph * 1.002 and (price - ph) >= 0.3 * a_i
                    rsi_ok = r < 80
                    if prev_close_le and real_break and rsi_ok:
                        long_trig = True
                if dn_ok and not np.isnan(pl) and atr_exp:
                    prev_close_ge = close[i - 1] >= pl
                    real_break = price < pl * 0.998 and (pl - price) >= 0.3 * a_i
                    rsi_ok = r > 20
                    if prev_close_ge and real_break and rsi_ok:
                        short_trig = True

        if long_trig or short_trig:
            side = "long" if long_trig else "short"
            if INVERT_SIDE:
                side = "short" if side == "long" else "long"
            margin = 10000 * 0.15
            notional = margin * leverage
            size = notional / price
            # pullback_in_regime uses flat 3% SL (not ATR-based) since entries
            # are at structural levels, not volatility-scaled.
            if cfg.entry_type == "pullback_in_regime":
                sl = price * 0.97 if side == "long" else price * 1.03
            else:
                sl = price - a_i * cfg.sl_atr if side == "long" else price + a_i * cfg.sl_atr
            tp1 = (price + a_i * cfg.tp1_atr if side == "long" else price - a_i * cfg.tp1_atr) if cfg.tp1_atr > 0 else None
            tp2 = (price + a_i * cfg.tp2_atr if side == "long" else price - a_i * cfg.tp2_atr) if cfg.tp2_atr > 0 else None
            tp3 = (price + a_i * cfg.tp3_atr if side == "long" else price - a_i * cfg.tp3_atr) if cfg.tp3_atr > 0 else None
            trail = a_i * cfg.trail_atr if cfg.trail_atr > 0 else 0.0
            disaster = 0.0
            if cfg.disaster_sl_atr > 0:
                disaster = (price - a_i * cfg.disaster_sl_atr) if side == "long" \
                           else (price + a_i * cfg.disaster_sl_atr)
            position = dict(entry=price, side=side, size=size, notional=notional,
                            sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                            trail_offset=trail, trail_active=False, best=price, entry_bar=i,
                            entry_atr=a_i, max_fav=0.0, disaster_sl=disaster)
            tp1_hit = tp2_hit = tp3_hit = False
    return trades


def stats(trades):
    if not trades:
        return dict(n=0, pnl=0.0, wr=0.0, pf=None, dd=0.0, avg=0.0)
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    wr = 100 * len(wins) / len(pnls)
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else None
    cum = np.cumsum(pnls) + 10000
    peaks = np.maximum.accumulate(cum)
    dd = float(((cum - peaks) / peaks * 100).min())
    return dict(
        n=len(pnls),
        pnl=float(round(pnls.sum(), 2)),
        wr=float(round(wr, 1)),
        pf=float(round(pf, 2)) if pf else None,
        dd=round(dd, 2),
        avg=float(round(pnls.mean(), 2)),
    )


def grid():
    # Kept focused — 4 entries × 2 1h filters × 2 SLs × 2 trails = 32 per symbol
    entries = ["bb_touch", "ema_bounce", "rsi_bounce", "swing_pivot"]
    filter_1h = ["ema_cross", "both_agree"]
    sls = [1.5, 2.0]
    trails = [0.0, 1.5]
    for et, f1h, sl, tr in product(entries, filter_1h, sls, trails):
        yield Cfg(
            trend_filter="ema_slope",
            entry_type=et,
            rsi_oversold=35.0, rsi_overbought=65.0,
            sl_atr=sl,
            tp1_atr=2.0, tp1_pct=0.3,
            tp2_atr=3.0, tp2_pct=0.3,
            tp3_atr=4.0, tp3_pct=0.2,
            trail_atr=tr,
            max_hold_bars=480,  # 15m bars ≈ 5 days
            direction="both",
            use_1h_filter=True,
            trend_filter_1h=f1h,
            require_4h_agreement=False,
        )


def main():
    print(f"[1/3] Fetching 15m / 1h / 4h candles for {len(SYMBOLS)} commodities...")
    raw = {}
    for sym in SYMBOLS:
        d15 = add_features(fetch_hl(sym, "15m", 4000))
        d1h = add_features(fetch_hl(sym, "1h", 2000))
        d4h = add_features(fetch_hl(sym, "4h", 1000))
        if len(d15) < 250 or len(d1h) < 100:
            print(f"   {sym}: too little data (15m={len(d15)}, 1h={len(d1h)}) — skip")
            continue
        arr = precompute(d15, d1h, d4h)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        wd_pct = float(arr["weekday"].mean() * 100)
        print(f"   {sym:<14} 15m={len(d15)} ({days}d)  1h={len(d1h)}  4h={len(d4h)}  weekday={wd_pct:.0f}%")
        raw[sym] = (d15, arr)

    print(f"\n[2/3] Running grid = {sum(1 for _ in grid())} configs per symbol × {len(raw)} symbols")
    results = {}
    for sym, (d15, arr) in raw.items():
        lev = LEV_CAP[sym] * 0.15
        best = None
        all_runs = []
        for cfg in grid():
            trades = backtest(arr, cfg, lev)
            s = stats(trades)
            row = {**s, "entry_type": cfg.entry_type,
                   "trend_filter_1h": cfg.trend_filter_1h,
                   "sl_atr": cfg.sl_atr, "trail_atr": cfg.trail_atr}
            all_runs.append(row)
            if s["pf"] is None or s["n"] < 15:
                continue
            if best is None or (s["pf"], s["pnl"]) > (best["pf"] or 0, best["pnl"]):
                best = row
        results[sym] = {"best": best, "runs": all_runs}
        print(f"\n{sym}:")
        if best:
            print(f"  BEST: {best['entry_type']:<12} 1h={best['trend_filter_1h']:<11} "
                  f"sl={best['sl_atr']} trail={best['trail_atr']}  "
                  f"n={best['n']} pnl=${best['pnl']:+.0f} wr={best['wr']}% "
                  f"pf={best['pf']} dd={best['dd']}%")
        else:
            print("  no config met minimum trade count / profitability threshold")

    print(f"\n[3/3] Writing full results to /tmp/commod_backtest.json")
    with open("/tmp/commod_backtest.json", "w") as f:
        json.dump(results, f, indent=2, default=str)


if __name__ == "__main__":
    main()
