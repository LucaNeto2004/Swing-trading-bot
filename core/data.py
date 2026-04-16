"""HyperLiquid candle fetcher + feature pipeline per symbol."""
import time
from typing import Optional

import numpy as np
import pandas as pd
import requests

from config.settings import BotConfig
from core.features import add_features, trend_lookup_1h
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
        self.up_1h: dict[str, np.ndarray] = {}
        self.dn_1h: dict[str, np.ndarray] = {}
        self.last_5m_ts: dict[str, pd.Timestamp] = {}

    def refresh(self, symbol: str) -> bool:
        """Re-fetch 5m + 1h for a symbol. Returns True if the latest 5m bar is new."""
        d5 = fetch_candles(symbol, self.config.entry_tf, self.config.lookback_5m)
        d1 = fetch_candles(symbol, self.config.trend_tf, self.config.lookback_1h)
        if d5.empty:
            log.warning(f"{symbol}: empty 5m fetch")
            return False
        d5 = add_features(d5)
        d1 = add_features(d1) if not d1.empty else d1
        self.df_5m[symbol] = d5
        self.df_1h[symbol] = d1
        up, dn = trend_lookup_1h(d5, d1)
        self.up_1h[symbol] = up
        self.dn_1h[symbol] = dn
        latest_ts = d5['timestamp'].iloc[-1]
        is_new = self.last_5m_ts.get(symbol) != latest_ts
        self.last_5m_ts[symbol] = latest_ts
        return is_new

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
