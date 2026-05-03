"""Intensive backtest research on a target symbol or list of symbols.

Designed for the question "can we save this losing symbol with better params?"
Expands beyond commod_oos.py's grid:

  - Adds entry_type=ensemble_regime (K∈{3,4,5}, ±BOS) — the entry style every
    actually-passing deployed config uses, conspicuously absent from commod_oos.
  - Tests direction=both AND long_only separately (BTC analysis showed live
    shorts were a real loser even when "both" was deployed).
  - 5 1h filters (ema_cross, hma_slope, sjm, structure, both_agree) — same set
    available in commod_backtest.
  - Wider SL grid (1.5 / 2.0 / 2.5).
  - Tests with + without 4h agreement gate.

Total grid is ~150-300 configs per symbol. Runtime ~5-10 min/symbol on 41d data.

Applies the same OOS gate as commod_oos.py:
  OOS n>=10, PF>=1.2, PnL>0, ≤1 quartile negative, PF>=random+0.3,
  ±20% sensitivity holds.

Usage:
  python research/intensive_grid.py --syms TIA OP ARB FARTCOIN LIT xyz:SILVER
  python research/intensive_grid.py --syms TIA  # single
  python research/intensive_grid.py --losers    # default loser+dormant set
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request
from itertools import product
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

import research.commod_backtest as cb
import research.commod_oos as oos
from research.commod_backtest import Cfg, fetch_hl, add_features, precompute, backtest, stats

DEFAULT_LOSERS = ["TIA", "OP", "ARB", "FARTCOIN", "LIT", "xyz:SILVER"]


def hl_max_leverage(symbol: str) -> int:
    """Pull max leverage for a symbol from HL meta. Falls back to 5 if not found."""
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "meta"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        meta = json.load(urllib.request.urlopen(req, timeout=10))
        for u in meta.get("universe", []):
            if u.get("name") == symbol:
                return int(u.get("maxLeverage") or 5)
    except Exception:
        pass
    return 5


def expanded_grid():
    """Yield configs covering ensemble_regime + the bb/ema/rsi/swing entries."""
    # ============ Pullback-style entries (commod_oos baseline) ============
    pullback_entries = ["bb_touch", "ema_bounce", "rsi_bounce", "swing_pivot"]
    filter_1h = ["ema_cross", "hma_slope", "sjm", "structure", "both_agree"]
    sls = [1.5, 2.0, 2.5]
    trails = [0.0, 1.5]
    directions = ["both", "long_only"]
    use_4h = [False, True]

    for et, f1h, sl, tr, dr, fh in product(
        pullback_entries, filter_1h, sls, trails, directions, use_4h
    ):
        yield Cfg(
            trend_filter="ema_slope", entry_type=et,
            rsi_oversold=35.0, rsi_overbought=65.0,
            sl_atr=sl, tp1_atr=2.0, tp1_pct=0.3,
            tp2_atr=3.0, tp2_pct=0.3, tp3_atr=4.0, tp3_pct=0.2,
            trail_atr=tr, max_hold_bars=480, direction=dr,
            use_1h_filter=True, trend_filter_1h=f1h,
            require_4h_agreement=fh, exit_type="standard",
        )

    # ============ Ensemble regime entries (the missing entry type) ============
    # BTC, ETH, ZEC, ENA all use ensemble_regime — clear successful pattern.
    for k, bos, f1h, sl, dr in product(
        [3, 4, 5], [False, True], filter_1h, [1.5, 2.0, 2.5], directions
    ):
        yield Cfg(
            trend_filter="ema_slope", entry_type="ensemble_regime",
            rsi_oversold=30.0, rsi_overbought=70.0,
            sl_atr=sl, tp1_atr=3.0, tp1_pct=0.3,
            tp2_atr=4.5, tp2_pct=0.3, tp3_atr=6.0, tp3_pct=0.2,
            trail_atr=2.5, max_hold_bars=1000, direction=dr,
            use_1h_filter=True, trend_filter_1h=f1h,
            require_4h_agreement=False,
            ensemble_k=k, require_bos_confirm=bos,
            exit_type="ensemble_hybrid",
        )


def fetch_for(sym):
    d15 = add_features(fetch_hl(sym, "15m", 4000))
    d1h = add_features(fetch_hl(sym, "1h", 2000))
    d4h = add_features(fetch_hl(sym, "4h", 1000))
    if len(d15) < 500 or len(d1h) < 100:
        return None
    arr = precompute(d15, d1h, d4h)
    if not sym.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
    return d15, arr


def run_symbol(sym):
    print(f"\n{'='*78}\n{sym}\n{'='*78}")
    fetched = fetch_for(sym)
    if fetched is None:
        return {"symbol": sym, "elected": None, "reason": "insufficient data"}
    d15, arr = fetched
    days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
    print(f"  data: 15m={len(d15)} ({days}d)")

    lev = hl_max_leverage(sym) * 0.15
    print(f"  effective lev (margin_pct × hl_max): {lev:.2f}x")

    n = len(arr["close"])
    is_end = int(n * 0.7)
    print(f"  IS=[52..{is_end})  OOS=[{is_end}..{n})")

    # Phase 1: rank by IS PF (must have >=10 IS trades)
    is_runs = []
    grid_list = list(expanded_grid())
    print(f"  Phase 1: scanning {len(grid_list)} configs on IS...")
    for cfg in grid_list:
        try:
            tr_is = oos.bt_range(arr, cfg, lev, i_start=52, i_end=is_end)
        except Exception:
            continue
        s = stats(tr_is)
        if s["n"] < 10 or not s["pf"]:
            continue
        is_runs.append((cfg, s))
    if not is_runs:
        print(f"  → no IS-eligible configs")
        return {"symbol": sym, "elected": None, "reason": "no IS-eligible config"}
    is_runs.sort(key=lambda cs: (cs[1]["pf"], cs[1]["pnl"]), reverse=True)
    print(f"  Phase 1: {len(is_runs)} eligible IS configs")

    # Phase 2: full OOS validation on top 20 IS performers
    candidates = []
    for cfg, is_s in is_runs[:20]:
        try:
            all_trades = backtest(arr, cfg, lev)
        except Exception:
            continue
        oos_cutoff = arr["timestamp"][is_end]
        oos_trades = [t for t in all_trades if t["ts"] >= oos_cutoff]
        oos_s = stats(oos_trades)
        quartiles = oos.quartile_split(all_trades)
        rnd = oos.random_benchmark(arr, cfg, lev)
        sens = oos.sensitivity(arr, cfg, lev)
        v = oos.verdict(oos_s, quartiles, rnd, sens)
        candidates.append({
            "cfg": cfg, "is": is_s, "oos": oos_s, "full": stats(all_trades),
            "quartiles": quartiles, "random": rnd, "sensitivity": sens,
            "verdict": v,
        })

    passing = [c for c in candidates if c["verdict"]["pass"]]
    print(f"  Phase 2: {len(passing)} of top {len(candidates)} pass full OOS gate")

    if not passing:
        # Show the closest-to-passing config for diagnostic
        if candidates:
            best = max(candidates, key=lambda c: (c["oos"]["pnl"], c["oos"]["pf"] or 0))
            print(f"  Best non-passing (diagnostic):")
            cfg = best["cfg"]
            print(f"    {cfg.entry_type:<18} k={cfg.ensemble_k} bos={cfg.require_bos_confirm} "
                  f"dir={cfg.direction:<10} 1h={cfg.trend_filter_1h:<11} sl={cfg.sl_atr}")
            print(f"    IS pf={best['is']['pf']} pnl=${best['is']['pnl']:+.0f}  "
                  f"OOS n={best['oos']['n']} pf={best['oos']['pf']} pnl=${best['oos']['pnl']:+.0f}")
            print(f"    fail reasons: {best['verdict']['fail_reasons']}")
        return {"symbol": sym, "elected": None,
                "best_attempt": _serialize_cand(candidates[0]) if candidates else None,
                "reason": "no config passes OOS gate"}

    # Pick best by OOS PF then PnL
    elect = max(passing, key=lambda c: (c["oos"]["pf"] or 0, c["oos"]["pnl"]))
    cfg = elect["cfg"]
    print(f"  ✓ ELECTED: {cfg.entry_type} k={cfg.ensemble_k} dir={cfg.direction} "
          f"1h={cfg.trend_filter_1h} sl={cfg.sl_atr}")
    print(f"    IS  n={elect['is']['n']} pnl=${elect['is']['pnl']:+.0f} pf={elect['is']['pf']}")
    print(f"    OOS n={elect['oos']['n']} pnl=${elect['oos']['pnl']:+.0f} pf={elect['oos']['pf']}")
    print(f"    Random benchmark: pf={elect['random']['pf_avg']:.2f}")
    return {"symbol": sym, "elected": _serialize_cand(elect),
            "all_passing": [_serialize_cand(c) for c in passing[:5]]}


def _serialize_cand(c):
    cfg = c["cfg"]
    return {
        "cfg": {
            "entry_type": cfg.entry_type,
            "trend_filter_1h": cfg.trend_filter_1h,
            "sl_atr": cfg.sl_atr, "trail_atr": cfg.trail_atr,
            "tp1_atr": cfg.tp1_atr, "tp1_pct": cfg.tp1_pct,
            "tp2_atr": cfg.tp2_atr, "tp2_pct": cfg.tp2_pct,
            "tp3_atr": cfg.tp3_atr, "tp3_pct": cfg.tp3_pct,
            "rsi_oversold": cfg.rsi_oversold, "rsi_overbought": cfg.rsi_overbought,
            "max_hold_bars": cfg.max_hold_bars, "direction": cfg.direction,
            "use_1h_filter": cfg.use_1h_filter,
            "require_4h_agreement": cfg.require_4h_agreement,
            "ensemble_k": cfg.ensemble_k,
            "require_bos_confirm": cfg.require_bos_confirm,
            "exit_type": cfg.exit_type,
        },
        "is": c["is"], "oos": c["oos"], "full": c["full"],
        "verdict": c["verdict"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", nargs="+", default=None)
    ap.add_argument("--losers", action="store_true",
                    help="run default loser/dormant set: " + ", ".join(DEFAULT_LOSERS))
    ap.add_argument("--out", default="/tmp/intensive_grid.json")
    args = ap.parse_args()

    syms = args.syms or (DEFAULT_LOSERS if args.losers else None)
    if not syms:
        print("Specify --syms <SYM> ... or --losers")
        sys.exit(1)
    print(f"Targeting: {syms}\n")

    results = []
    for sym in syms:
        try:
            results.append(run_symbol(sym))
        except Exception as e:
            print(f"  {sym}: ERROR {e}")
            results.append({"symbol": sym, "elected": None, "reason": f"error: {e}"})

    # Summary
    print(f"\n{'='*78}\nSUMMARY\n{'='*78}")
    print(f"{'SYM':<14} {'STATUS':<8}  ELECTED CONFIG / NOTE")
    print("-" * 100)
    for r in results:
        if r["elected"]:
            cfg = r["elected"]["cfg"]
            o = r["elected"]["oos"]
            cfg_str = (f"{cfg['entry_type']}·k{cfg.get('ensemble_k','-')}·"
                       f"{cfg['direction']}·{cfg['trend_filter_1h']}·sl{cfg['sl_atr']}")
            print(f"{r['symbol']:<14} PASS ✓   OOS pf={o['pf']} ${o['pnl']:+.0f} (n={o['n']})  {cfg_str}")
        else:
            print(f"{r['symbol']:<14} FAIL ✗   {r.get('reason','')}")

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results → {args.out}")


if __name__ == "__main__":
    main()
