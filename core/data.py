"""HyperLiquid candle fetcher + feature pipeline per symbol."""
import time
from typing import Optional

import numpy as np
import pandas as pd
import requests

from config.settings import BotConfig
from core.features import add_features, trend_lookup_1h, structure_lookup_1h, hma_slope_lookup_1h, sjm_lookup_1h, kalman_slope_lookup_1h, last_pivot_levels_lookup_1h
from core.quant_filters import (
    rolling_hurst, compute_adx, combined_pivots_1h, align_1h_to_entry,
)
from utils.logger import setup_logger

log = setup_logger("data")

HL_API = "https://api.hyperliquid.xyz/info"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def fetch_candles(symbol: str, interval: str, bars: int, retries: int = 3) -> pd.DataFrame:
    """Fetch OHLCV candles from HL. Returns a DataFrame with features applied."""
    ms = INTERVAL_MS[interval]
    end = int(time.time() * 1000)
    start = end - ms * bars
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": symbol, "interval": interval, "startTime": start, "endTime": end},
    }
    for attempt in range(retries):
        try:
            r = requests.post(HL_API, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                log.warning(f"{symbol} {interval}: HTTP {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            raw = r.json() or []
            if not raw:
                return pd.DataFrame()
            df = pd.DataFrame([{
                'timestamp': pd.to_datetime(int(c['t']), unit='ms', utc=True),
                'open': float(c['o']), 'high': float(c['h']),
                'low': float(c['l']), 'close': float(c['c']),
                'volume': float(c['v']),
            } for c in raw])
            df = df.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
            return df
        except Exception as e:
            log.warning(f"{symbol} {interval} fetch error: {e}")
            time.sleep(2 ** attempt)
    return pd.DataFrame()


class DataManager:
    """Caches 5m + 1h features per symbol + the 1h trend lookup array."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.df_5m: dict[str, pd.DataFrame] = {}
        self.df_1h: dict[str, pd.DataFrame] = {}
        self.df_4h: dict[str, pd.DataFrame] = {}
        # EMA-cross 1h filter (original)
        self.up_1h: dict[str, np.ndarray] = {}
        self.dn_1h: dict[str, np.ndarray] = {}
        # Structure-based 1h filter (ICT state machine)
        self.up_struct_1h: dict[str, np.ndarray] = {}
        self.dn_struct_1h: dict[str, np.ndarray] = {}
        # HMA-slope 1h filter (no pivot-confirmation lag)
        self.up_hma_1h: dict[str, np.ndarray] = {}
        self.dn_hma_1h: dict[str, np.ndarray] = {}
        # Statistical Jump Model 1h filter (Shu-Yu-Mulvey 2024)
        self.up_sjm_1h: dict[str, np.ndarray] = {}
        self.dn_sjm_1h: dict[str, np.ndarray] = {}
        # Kalman-filter velocity 1h filter (adaptive state-space on log-close)
        self.up_kalman_1h: dict[str, np.ndarray] = {}
        self.dn_kalman_1h: dict[str, np.ndarray] = {}
        # 1h pivot levels — most recent confirmed swing high/low aligned to 5m.
        # Used by BOS entry/exit logic: close > last_pivot_h → long BOS trigger.
        self.last_pivot_h_1h: dict[str, np.ndarray] = {}
        self.last_pivot_l_1h: dict[str, np.ndarray] = {}
        # Regime classifier inputs: Hurst + ADX (both 1h, aligned to 5m)
        self.hurst_1h: dict[str, np.ndarray] = {}
        self.adx_1h: dict[str, np.ndarray] = {}
        # Validated pivot events — boolean array, True only on confirmation bar
        self.valid_piv_h_event: dict[str, np.ndarray] = {}
        self.valid_piv_l_event: dict[str, np.ndarray] = {}
        # Structure-based 4h filter (HTF macro regime — opt-in per symbol)
        self.up_struct_4h: dict[str, np.ndarray] = {}
        self.dn_struct_4h: dict[str, np.ndarray] = {}
        # Fresh-BOS flags per direction — True on the 5m bar that first maps
        # to a 1h bar where the protected high/low just advanced in trend.
        # Used by pyramid logic to fire adds only on new structural breaks.
        self.bos_up_1h: dict[str, np.ndarray] = {}
        self.bos_dn_1h: dict[str, np.ndarray] = {}
        self.last_5m_ts: dict[str, pd.Timestamp] = {}
        # BTC 1h last-closed-bar direction (sign of log-return):
        #   +1 = BTC 1h up, -1 = down, 0 = flat/nan
        # Used by symbols that opt into require_btc_1h_confirm. Single shared
        # scalar (per refresh cycle) since BTC is a single leader for all symbols.
        self.btc_1h_dir: int = 0

    def refresh(self, symbol: str) -> bool:
        """Re-fetch 5m + 1h + 4h for a symbol. Returns True if the latest 5m bar is new."""
        d5 = fetch_candles(symbol, self.config.entry_tf, self.config.lookback_5m)
        d1 = fetch_candles(symbol, self.config.trend_tf, self.config.lookback_1h)
        # 4h: ~250 bars = 40 days of history is enough for pivot detection
        d4 = fetch_candles(symbol, "4h", 300)
        if d5.empty:
            log.warning(f"{symbol}: empty 5m fetch")
            return False
        d5 = add_features(d5)
        d1 = add_features(d1) if not d1.empty else d1
        d4 = add_features(d4) if not d4.empty else d4
        self.df_5m[symbol] = d5
        self.df_4h[symbol] = d4
        self.df_1h[symbol] = d1
        up, dn = trend_lookup_1h(d5, d1)
        self.up_1h[symbol] = up
        self.dn_1h[symbol] = dn
        up_s, dn_s, bos_u, bos_d = structure_lookup_1h(d5, d1, return_bos=True)
        self.up_struct_1h[symbol] = up_s
        self.dn_struct_1h[symbol] = dn_s
        self.bos_up_1h[symbol] = bos_u
        self.bos_dn_1h[symbol] = bos_d
        up_h, dn_h = hma_slope_lookup_1h(d5, d1)
        self.up_hma_1h[symbol] = up_h
        self.dn_hma_1h[symbol] = dn_h
        up_j, dn_j = sjm_lookup_1h(d5, d1)
        self.up_sjm_1h[symbol] = up_j
        self.dn_sjm_1h[symbol] = dn_j
        up_k, dn_k = kalman_slope_lookup_1h(d5, d1)
        self.up_kalman_1h[symbol] = up_k
        self.dn_kalman_1h[symbol] = dn_k
        ph, pl = last_pivot_levels_lookup_1h(d5, d1, lookback=3)
        self.last_pivot_h_1h[symbol] = ph
        self.last_pivot_l_1h[symbol] = pl
        # Quant regime filters — Hurst + ADX on 1h, aligned causally to 5m
        if len(d1) >= 50:
            ts_1h = pd.to_datetime(d1["timestamp"], utc=True).astype("int64").to_numpy()
            ts_5m = pd.to_datetime(d5["timestamp"], utc=True).astype("int64").to_numpy()
            hurst_raw = rolling_hurst(d1["close"].to_numpy(), window=100)
            adx_raw = compute_adx(d1["high"].to_numpy(), d1["low"].to_numpy(),
                                  d1["close"].to_numpy(), period=14)
            self.hurst_1h[symbol] = align_1h_to_entry(hurst_raw, ts_1h, ts_5m)
            self.adx_1h[symbol] = align_1h_to_entry(adx_raw, ts_1h, ts_5m)
            # Validated pivot events (fractal + HMA-smoothed, 1-validator + ATR gate)
            valid_h, valid_l = combined_pivots_1h(d1, df_4h=d4 if not d4.empty else None,
                                                   fractal_lookback=3, smoothed_lookback=2,
                                                   atr_min_move=1.0, vol_spike=1.3,
                                                   rsi_extreme=(35.0, 65.0))
            evt_h = np.zeros(len(d5), dtype=bool)
            evt_l = np.zeros(len(d5), dtype=bool)
            for confirm_ts, _, _, _ in valid_h:
                i = int(np.searchsorted(ts_5m, confirm_ts, side="left"))
                if i < len(d5): evt_h[i] = True
            for confirm_ts, _, _, _ in valid_l:
                i = int(np.searchsorted(ts_5m, confirm_ts, side="left"))
                if i < len(d5): evt_l[i] = True
            self.valid_piv_h_event[symbol] = evt_h
            self.valid_piv_l_event[symbol] = evt_l
        else:
            n5 = len(d5) if d5 is not None else 0
            self.hurst_1h[symbol] = np.full(n5, np.nan)
            self.adx_1h[symbol] = np.full(n5, np.nan)
            self.valid_piv_h_event[symbol] = np.zeros(n5, dtype=bool)
            self.valid_piv_l_event[symbol] = np.zeros(n5, dtype=bool)
        # 4h structure (HTF regime gate) — same ICT state machine, 4h data.
        # Uses pivot_bars=3 because 4h has fewer bars per same wallclock.
        if not d4.empty:
            up_4h, dn_4h = structure_lookup_1h(d5, d4, pivot_bars=3)
        else:
            up_4h = np.ones(len(d5), dtype=bool)
            dn_4h = np.ones(len(d5), dtype=bool)
        self.up_struct_4h[symbol] = up_4h
        self.dn_struct_4h[symbol] = dn_4h
        # Cache BTC 1h direction when we refresh BTC. Other symbols read
        # self.btc_1h_dir when they opt into require_btc_1h_confirm.
        if symbol == "BTC" and len(d1) >= 2:
            prev_close = float(d1['close'].iloc[-2])
            last_close = float(d1['close'].iloc[-1])
            if prev_close > 0 and last_close > 0 and last_close != prev_close:
                self.btc_1h_dir = 1 if last_close > prev_close else -1
            else:
                self.btc_1h_dir = 0
        latest_ts = d5['timestamp'].iloc[-1]
        is_new = self.last_5m_ts.get(symbol) != latest_ts
        self.last_5m_ts[symbol] = latest_ts
        return is_new

    def latest_1h_regime(self, symbol: str, variant: str) -> tuple[bool, bool]:
        """Return (up, dn) for the last 5m bar under the given 1h filter variant.

        One dispatch point for every filter choice — replaces repeated if/elif
        blocks in main.py and commod_backtest.py. Unknown variant falls back
        to ema_cross. If the array is missing (symbol not refreshed), both
        flags default to True (neutral, pass-through)."""
        def last(arr):
            return bool(arr[-1]) if arr is not None and len(arr) else True

        up_e = self.up_1h.get(symbol);       dn_e = self.dn_1h.get(symbol)
        up_s = self.up_struct_1h.get(symbol); dn_s = self.dn_struct_1h.get(symbol)
        up_h = self.up_hma_1h.get(symbol);   dn_h = self.dn_hma_1h.get(symbol)
        up_j = self.up_sjm_1h.get(symbol);   dn_j = self.dn_sjm_1h.get(symbol)
        up_k = self.up_kalman_1h.get(symbol); dn_k = self.dn_kalman_1h.get(symbol)

        if variant == "structure":
            return last(up_s), last(dn_s)
        if variant == "both_agree":
            return last(up_e) and last(up_s), last(dn_e) and last(dn_s)
        if variant == "hma_slope":
            return last(up_h), last(dn_h)
        if variant == "sjm":
            return last(up_j), last(dn_j)
        if variant == "kalman":
            return last(up_k), last(dn_k)
        return last(up_e), last(dn_e)  # ema_cross or unknown

    def latest_ensemble_counts(self, symbol: str) -> tuple[int, int, int, int]:
        """Return (up_cnt, dn_cnt, up_cnt_prev, dn_cnt_prev) — counts of how
        many of the 5 1h filters (ema_cross, structure, hma_slope, sjm,
        kalman) agree with UP/DN at the latest 5m bar, and at the previous
        5m bar.

        Used by entry_type=="ensemble_regime" for transition detection and by
        exit_type=="ensemble_hybrid" for consensus-drop exits. Returns zeros
        if arrays aren't populated (symbol not refreshed yet)."""
        arrs = [
            (self.up_1h.get(symbol), self.dn_1h.get(symbol)),
            (self.up_struct_1h.get(symbol), self.dn_struct_1h.get(symbol)),
            (self.up_hma_1h.get(symbol), self.dn_hma_1h.get(symbol)),
            (self.up_sjm_1h.get(symbol), self.dn_sjm_1h.get(symbol)),
            (self.up_kalman_1h.get(symbol), self.dn_kalman_1h.get(symbol)),
        ]
        up_cnt = dn_cnt = up_cnt_prev = dn_cnt_prev = 0
        for up_a, dn_a in arrs:
            if up_a is not None and len(up_a):
                up_cnt += int(bool(up_a[-1]))
                if len(up_a) >= 2:
                    up_cnt_prev += int(bool(up_a[-2]))
            if dn_a is not None and len(dn_a):
                dn_cnt += int(bool(dn_a[-1]))
                if len(dn_a) >= 2:
                    dn_cnt_prev += int(bool(dn_a[-2]))
        return up_cnt, dn_cnt, up_cnt_prev, dn_cnt_prev

    def latest_regime_label(self, symbol: str) -> str:
        """Classify the current regime from Hurst + ADX + 5-filter ensemble vote.

        Returns one of: trend_up | trend_down | range | chop.
        Mirrors the logic in research/commod_backtest.py."""
        up_cnt, dn_cnt, _, _ = self.latest_ensemble_counts(symbol)
        vote = up_cnt - dn_cnt
        h_arr = self.hurst_1h.get(symbol); a_arr = self.adx_1h.get(symbol)
        if h_arr is None or a_arr is None or len(h_arr) == 0 or len(a_arr) == 0:
            return "chop"
        hurst = float(h_arr[-1]); adx_v = float(a_arr[-1])
        if np.isnan(hurst) or np.isnan(adx_v):
            hurst = 0.5; adx_v = 20.0
        if vote >= 2 and (hurst > 0.5 or adx_v > 18):
            return "trend_up"
        if vote <= -2 and (hurst > 0.5 or adx_v > 18):
            return "trend_down"
        if abs(vote) <= 2 and hurst < 0.5 and adx_v < 25:
            return "range"
        return "chop"

    def latest_pivot_event(self, symbol: str) -> tuple[bool, bool]:
        """Returns (pivot_h_just_confirmed, pivot_l_just_confirmed) at the
        latest 5m bar. Used by pullback_in_regime entry + pullback_exit."""
        eh = self.valid_piv_h_event.get(symbol)
        el = self.valid_piv_l_event.get(symbol)
        h = bool(eh[-1]) if eh is not None and len(eh) else False
        l = bool(el[-1]) if el is not None and len(el) else False
        return h, l

    def latest_4h_regime(self, symbol: str) -> tuple[bool, bool]:
        """4h HTF structure filter. Returns (up, dn) pair."""
        up4 = self.up_struct_4h.get(symbol); dn4 = self.dn_struct_4h.get(symbol)
        def last(arr): return bool(arr[-1]) if arr is not None and len(arr) else True
        return last(up4), last(dn4)

    def latest_price(self, symbol: str) -> Optional[float]:
        df = self.df_5m.get(symbol)
        if df is None or df.empty:
            return None
        return float(df['close'].iloc[-1])

    def latest_bar(self, symbol: str) -> Optional[pd.Series]:
        df = self.df_5m.get(symbol)
        if df is None or df.empty:
            return None
        return df.iloc[-1]
