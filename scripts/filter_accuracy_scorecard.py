"""Per-symbol filter-accuracy backtest.

For every 5m bar in the last N days, record what each of the 4 1h filters +
the 4h structure filter said about direction, then measure the forward return
at 4h / 24h / 72h. Score: % of times filter's UP-call had a positive forward
return (and DOWN-call had a negative forward return).

High hit rate = filter is predictive. Low hit rate (near 50%) = filter is
random noise for that symbol. Biased low = filter is *contrarian* (signals
the opposite of what happens).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from core.features import add_features, trend_lookup_1h, structure_lookup_1h, hma_slope_lookup_1h, sjm_lookup_1h

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "FARTCOIN", "LIT", "ENA", "SOL"]
# xyz:CL skipped here — xyz-prefixed perps often return different candle data
# and this is a crypto-regime scorecard.
LOOKBACK_DAYS = 45
HORIZONS_BARS = {"4h": 48, "24h": 288, "72h": 864}  # in 5m bars


def fetch_candles_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    payload = {"type": "candleSnapshot",
               "req": {"coin": symbol, "interval": interval,
                       "startTime": start_ms, "endTime": end_ms}}
    for attempt in range(5):
        r = httpx.post(HL_INFO_URL, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt); continue
        r.raise_for_status()
        raw = r.json() or []
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "timestamp": pd.to_datetime(int(c["t"]), unit="ms", utc=True),
            "open": float(c["o"]), "high": float(c["h"]),
            "low": float(c["l"]), "close": float(c["c"]),
            "volume": float(c["v"])} for c in raw])
        return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return pd.DataFrame()


def score_filter(up_arr, dn_arr, returns):
    """Compute hit rate for UP-calls, DOWN-calls, and neutral/off bars.
    returns: forward percent returns, same length as up_arr/dn_arr.
    A hit = filter says UP and ret>0, or filter says DN and ret<0.
    """
    up_arr = np.asarray(up_arr, dtype=bool)
    dn_arr = np.asarray(dn_arr, dtype=bool)
    returns = np.asarray(returns, dtype=float)
    valid = ~np.isnan(returns)

    up_mask = up_arr & ~dn_arr & valid
    dn_mask = dn_arr & ~up_arr & valid
    both_mask = up_arr & dn_arr & valid
    none_mask = ~up_arr & ~dn_arr & valid

    def hit_rate(mask, direction: str):
        if mask.sum() == 0:
            return None, 0, 0.0
        rets = returns[mask]
        if direction == "up":
            hits = (rets > 0).sum()
        else:
            hits = (rets < 0).sum()
        return hits / mask.sum(), mask.sum(), float(np.mean(rets))

    up_hit, up_n, up_mean = hit_rate(up_mask, "up")
    dn_hit, dn_n, dn_mean = hit_rate(dn_mask, "dn")
    return {
        "up_hit_rate": up_hit, "up_n": up_n, "up_mean_fwd_ret_pct": up_mean,
        "dn_hit_rate": dn_hit, "dn_n": dn_n, "dn_mean_fwd_ret_pct": dn_mean,
        "both_n": int(both_mask.sum()), "none_n": int(none_mask.sum()),
    }


def analyze(symbol: str) -> dict:
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - LOOKBACK_DAYS * 86400 * 1000
    d5 = fetch_candles_range(symbol, "5m", start, end)
    d1 = fetch_candles_range(symbol, "1h", start, end)
    d4 = fetch_candles_range(symbol, "4h", start - 30 * 86400 * 1000, end)  # extra 4h history
    if d5.empty or d1.empty:
        return {"error": f"{symbol}: empty candles"}
    d5 = add_features(d5); d1 = add_features(d1)
    d4 = add_features(d4) if not d4.empty else d4

    up_e, dn_e = trend_lookup_1h(d5, d1)
    up_s, dn_s = structure_lookup_1h(d5, d1)
    up_h, dn_h = hma_slope_lookup_1h(d5, d1)
    up_j, dn_j = sjm_lookup_1h(d5, d1)
    up_4h, dn_4h = None, None
    if not d4.empty:
        up_4h, dn_4h = structure_lookup_1h(d5, d4, pivot_bars=3)

    closes = d5["close"].values
    result = {"symbol": symbol, "n_bars": len(d5)}
    for h_label, h_bars in HORIZONS_BARS.items():
        if len(closes) <= h_bars:
            continue
        fwd_ret = np.full(len(closes), np.nan)
        fwd_ret[:-h_bars] = (closes[h_bars:] / closes[:-h_bars] - 1) * 100
        h_result = {}
        h_result["ema_cross"] = score_filter(up_e, dn_e, fwd_ret)
        h_result["structure_1h"] = score_filter(up_s, dn_s, fwd_ret)
        h_result["hma_slope"] = score_filter(up_h, dn_h, fwd_ret)
        h_result["sjm"] = score_filter(up_j, dn_j, fwd_ret)
        if up_4h is not None:
            h_result["structure_4h"] = score_filter(up_4h, dn_4h, fwd_ret)
        result[h_label] = h_result
    return result


def print_scorecard(results: list[dict]):
    for h_label in HORIZONS_BARS.keys():
        print(f"\n{'='*96}")
        print(f"  FORWARD-RETURN HIT RATE at {h_label} horizon")
        print(f"  (UP-call right = price higher; DOWN-call right = price lower)")
        print('='*96)
        print(f"\n{'SYMBOL':<10} {'FILTER':<14} {'UP hit%':>9} {'UP n':>7} {'UP mean%':>10} "
              f"{'DN hit%':>9} {'DN n':>7} {'DN mean%':>10}")
        print("-" * 96)
        for r in results:
            if "error" in r or h_label not in r:
                continue
            for fname in ("ema_cross", "structure_1h", "hma_slope", "sjm", "structure_4h"):
                if fname not in r[h_label]:
                    continue
                s = r[h_label][fname]
                up_pct = f"{s['up_hit_rate']*100:.1f}" if s['up_hit_rate'] is not None else "—"
                dn_pct = f"{s['dn_hit_rate']*100:.1f}" if s['dn_hit_rate'] is not None else "—"
                up_m = f"{s['up_mean_fwd_ret_pct']:+.3f}" if s['up_n'] else "—"
                dn_m = f"{s['dn_mean_fwd_ret_pct']:+.3f}" if s['dn_n'] else "—"
                print(f"{r['symbol']:<10} {fname:<14} {up_pct:>9} {s['up_n']:>7} {up_m:>10} "
                      f"{dn_pct:>9} {s['dn_n']:>7} {dn_m:>10}")
            print()


def main():
    results = []
    for sym in SYMBOLS:
        print(f"Fetching {sym}...", flush=True)
        try:
            results.append(analyze(sym))
        except Exception as e:
            print(f"  {sym} failed: {e}")
            results.append({"symbol": sym, "error": str(e)})
    print_scorecard(results)


if __name__ == "__main__":
    main()
