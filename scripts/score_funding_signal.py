"""Hit-rate scorecard for funding-rate signals across all symbols.

Tests the crowded-longs-get-flushed hypothesis: when funding is at the
upper extreme (z24h >= 2.0), is the forward return negative? And vice
versa for negative extremes.

If yes — funding extremes are a contrarian signal we can add as a filter.
If no (or inverse) — it's a momentum signal. Either way informs the gate.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_bot_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_shared = os.path.join(_bot_root, "shared")
if not os.path.isdir(_shared):
    _shared = os.path.abspath(os.path.join(_bot_root, "..", "shared"))
sys.path.insert(0, _shared)

import numpy as np
import pandas as pd

from core.features import add_funding_features
from scripts.filter_accuracy_scorecard import fetch_candles_range, SYMBOLS
import hl_client  # noqa: E402

LOOKBACK_DAYS = 30
HORIZONS_BARS = {"4h": 48, "24h": 288, "72h": 864}


def score_extreme(extreme_arr, returns):
    """For each bar where funding is extreme (+1 or -1), score whether the
    contrarian direction would have been right. Returns hit-rate + mean return
    for positive-extreme and negative-extreme bars separately."""
    extreme_arr = np.asarray(extreme_arr, dtype=int)
    returns = np.asarray(returns, dtype=float)
    valid = ~np.isnan(returns)

    pos_mask = (extreme_arr == 1) & valid   # crowded longs
    neg_mask = (extreme_arr == -1) & valid  # crowded shorts

    def pack(mask, contrarian_dir: str):
        if mask.sum() == 0:
            return {"n": 0, "hit_rate": None, "mean_ret_pct": None}
        rets = returns[mask]
        if contrarian_dir == "down":
            hits = (rets < 0).sum()
        else:
            hits = (rets > 0).sum()
        return {"n": int(mask.sum()),
                "hit_rate": float(hits / mask.sum()),
                "mean_ret_pct": float(np.mean(rets))}

    return {
        "crowded_longs":  pack(pos_mask, "down"),   # extreme+ → hope for DN
        "crowded_shorts": pack(neg_mask, "up"),     # extreme- → hope for UP
    }


def analyze(symbol: str) -> dict:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - LOOKBACK_DAYS * 86400 * 1000
    d5 = fetch_candles_range(symbol, "5m", start_ms, end_ms)
    if d5.empty:
        return {"error": f"{symbol}: no candles"}
    funding = hl_client.sync_get_funding_history(symbol, start_ms, end_ms)
    if funding.empty:
        return {"error": f"{symbol}: no funding history"}

    enriched = add_funding_features(d5, funding)
    closes = enriched["close"].values

    result = {"symbol": symbol, "n_bars": len(enriched),
              "n_extreme_pos": int((enriched["funding_extreme"] == 1).sum()),
              "n_extreme_neg": int((enriched["funding_extreme"] == -1).sum())}
    for h_label, h_bars in HORIZONS_BARS.items():
        if len(closes) <= h_bars:
            continue
        fwd = np.full(len(closes), np.nan)
        fwd[:-h_bars] = (closes[h_bars:] / closes[:-h_bars] - 1) * 100
        result[h_label] = score_extreme(enriched["funding_extreme"].values, fwd)
    return result


def main():
    print(f"Scoring funding-extreme signals over {LOOKBACK_DAYS} days, all symbols.\n")
    results = []
    for sym in SYMBOLS:
        print(f"Fetching {sym}...", flush=True)
        try:
            r = analyze(sym)
            results.append(r)
            if "error" not in r:
                print(f"   {r['n_bars']} bars | extreme+={r['n_extreme_pos']}  extreme-={r['n_extreme_neg']}")
        except Exception as e:
            print(f"   {sym} failed: {e}")

    print(f"\n{'='*100}")
    print(f"  FUNDING-EXTREME contrarian hit rates")
    print(f"  crowded_longs  = bars with funding_z24h >= +2σ → hope forward return is NEGATIVE")
    print(f"  crowded_shorts = bars with funding_z24h <= −2σ → hope forward return is POSITIVE")
    print(f"{'='*100}")

    for h in HORIZONS_BARS.keys():
        print(f"\n  Horizon = {h}")
        print(f"  {'SYM':<10} {'crowdedLongs_n':>14} {'cL hit%':>9} {'cL meanRet%':>12} "
              f"{'cS_n':>6} {'cS hit%':>9} {'cS meanRet%':>12}")
        print("  " + "-" * 80)
        for r in results:
            if "error" in r or h not in r:
                continue
            cl = r[h]["crowded_longs"]
            cs = r[h]["crowded_shorts"]
            cl_hit = f"{cl['hit_rate']*100:.1f}" if cl["hit_rate"] is not None else "—"
            cl_ret = f"{cl['mean_ret_pct']:+.2f}" if cl["mean_ret_pct"] is not None else "—"
            cs_hit = f"{cs['hit_rate']*100:.1f}" if cs["hit_rate"] is not None else "—"
            cs_ret = f"{cs['mean_ret_pct']:+.2f}" if cs["mean_ret_pct"] is not None else "—"
            print(f"  {r['symbol']:<10} {cl['n']:>14} {cl_hit:>9} {cl_ret:>12} "
                  f"{cs['n']:>6} {cs_hit:>9} {cs_ret:>12}")


if __name__ == "__main__":
    main()
