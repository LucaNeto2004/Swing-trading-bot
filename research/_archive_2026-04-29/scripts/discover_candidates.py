"""Discover new candidate symbols from HyperLiquid's full perp universe.

Stage 1 of the discovery pipeline (this script):
  - Fetch HL meta — full list of perps with leverage caps + open interest + funding
  - Filter: leverage cap >= 5x, sufficient OI, not already deployed/retired
  - Rank by 24h volume × leverage cap (proxy for "tradeable opportunity")
  - Output top N candidates to /tmp/discovery_candidates.json

Stage 2 (separate run via discover_grid.py — TBD):
  - For each candidate, run commod_oos.py-style grid search
  - Apply OOS gate (PF >= 1.5, OOS PnL > 0, n >= 30, IS/OOS divergence < 3x)
  - Output ranked passing configs ready for deployment review

Usage:
  python research/discover_candidates.py            # default top 20
  python research/discover_candidates.py --top 40   # more candidates
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import INSTRUMENTS

DEPLOYED_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "deployed")
RETIRED_DIR = os.path.join(DEPLOYED_DIR, "_retired")
HL_INFO = "https://api.hyperliquid.xyz/info"

# Hand-curated exclusions: stablecoin-y, illiquid, deprecated
EXCLUDE = {"USDT", "USDC", "TUSD", "BUSD"}


def hl_post(body: dict, timeout: int = 15):
    req = urllib.request.Request(
        HL_INFO,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def already_deployed_or_retired() -> set[str]:
    """Symbols we've already evaluated (active or retired)."""
    seen = set()
    for d in (DEPLOYED_DIR, RETIRED_DIR):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.startswith("whale_") or not f.endswith(".json"):
                continue
            sym = f.replace("whale_", "").replace(".json", "")
            seen.add(sym.replace("_", ":"))  # whale_xyz_SILVER → xyz:SILVER
            seen.add(sym)
    return seen


def fetch_meta_and_assets():
    """Pull HL's full perp metadata + funding + open interest snapshot.

    Returns list of dicts with: symbol, max_lev, oi_usd, funding, mark_px, day_vol.
    """
    meta = hl_post({"type": "meta"})
    asset_ctx = hl_post({"type": "metaAndAssetCtxs"})
    universe = meta.get("universe", [])
    ctxs = asset_ctx[1] if len(asset_ctx) > 1 else []
    rows = []
    for u, c in zip(universe, ctxs):
        try:
            mark = float(c.get("markPx") or 0)
            oi_units = float(c.get("openInterest") or 0)
            day_ntl_vlm = float(c.get("dayNtlVlm") or 0)  # USD volume past 24h
            rows.append({
                "symbol": u.get("name"),
                "max_lev": int(u.get("maxLeverage") or 0),
                "mark_px": mark,
                "oi_usd": oi_units * mark,
                "funding": float(c.get("funding") or 0),
                "day_vol_usd": day_ntl_vlm,
                "delisted": bool(u.get("isDelisted") or False),
            })
        except Exception:
            continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="number of candidates to output")
    ap.add_argument("--min-lev", type=int, default=5, help="minimum max_leverage cap")
    ap.add_argument("--min-vol-musd", type=float, default=10.0, help="minimum 24h vol in $M")
    args = ap.parse_args()

    print(f"[1/3] Fetching HL universe...")
    rows = fetch_meta_and_assets()
    print(f"  {len(rows)} perps total")

    print(f"[2/3] Filtering...")
    seen = already_deployed_or_retired()
    print(f"  Already deployed/retired (skip): {sorted(seen)}")
    print(f"  Also in INSTRUMENTS dict: {sorted(INSTRUMENTS.keys())}")

    candidates = []
    for r in rows:
        sym = r["symbol"]
        if r.get("delisted"):
            continue
        if sym in EXCLUDE:
            continue
        if sym in seen or sym in INSTRUMENTS:
            continue
        if r["max_lev"] < args.min_lev:
            continue
        if r["day_vol_usd"] < args.min_vol_musd * 1_000_000:
            continue
        # Score: log-volume × leverage (heuristic — high vol + decent lev = tradeable edge)
        import math
        r["score"] = math.log10(max(1, r["day_vol_usd"])) * r["max_lev"]
        candidates.append(r)

    candidates.sort(key=lambda r: r["score"], reverse=True)
    top = candidates[:args.top]

    print(f"\n[3/3] Top {len(top)} candidates by (log_vol × max_lev):\n")
    print(f"{'sym':<12} {'lev':>4}x  {'24h vol':>12}  {'OI':>12}  funding")
    print("-" * 65)
    for r in top:
        vol_m = r["day_vol_usd"] / 1_000_000
        oi_m = r["oi_usd"] / 1_000_000
        print(f"{r['symbol']:<12} {r['max_lev']:>4}   ${vol_m:>9.1f}M  ${oi_m:>9.1f}M  {r['funding']*100:>+6.4f}%")

    out = "/tmp/discovery_candidates.json"
    with open(out, "w") as f:
        json.dump({
            "fetched_at": datetime.utcnow().isoformat(),
            "filters": {
                "min_lev": args.min_lev,
                "min_vol_musd": args.min_vol_musd,
                "excluded_already_seen": sorted(seen),
            },
            "candidates": top,
        }, f, indent=2, default=str)
    print(f"\n→ {out}")
    print(f"\nNext step: feed this list into a backtest grid (TBD: research/discover_grid.py)")
    print(f"       which runs commod_oos pipeline on each candidate and ranks by OOS edge.")


if __name__ == "__main__":
    main()
