"""Full per-symbol filter grid: 6 filter variants × 2 4h-gate states.

For every deployed symbol, hold entry_type / SL / TP / trail / direction fixed
(pinned at the deployed config) and vary ONLY:
  - trend_filter_1h ∈ {ema_cross, structure, both_agree, hma_slope, sjm, kalman}
  - require_4h_agreement ∈ {False, True}

= 12 variants per symbol. Split 70/30 IS/OOS. Scorecard gate:
  PASS if OOS PF >= 1.0 AND n >= 20 AND >= 3/4 quartiles positive AND full $ > 0

Output: for each symbol, (a) current deployed combo, (b) best-by-$ passing
combo, (c) delta. No auto-deploy — prints table, writes JSON.
"""
from __future__ import annotations

import os
import sys
import json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb


LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "ENA", "SOL", "xyz:CL"]
FILTER_VARIANTS = ["ema_cross", "structure", "both_agree", "hma_slope", "sjm", "kalman"]


def _patch_weekday(arr, symbol):
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def _cfg_from_deployed(d: dict) -> cb.Cfg:
    return cb.Cfg(
        trend_filter=d["trend_filter"], entry_type=d["entry_type"],
        rsi_oversold=float(d["rsi_oversold"]), rsi_overbought=float(d["rsi_overbought"]),
        sl_atr=float(d["sl_atr"]), tp1_atr=float(d["tp1_atr"]),
        tp1_pct=float(d["tp1_pct"]),
        tp2_atr=float(d.get("tp2_atr", 0.0)), tp2_pct=float(d.get("tp2_pct", 0.0)),
        tp3_atr=float(d.get("tp3_atr", 0.0)), tp3_pct=float(d.get("tp3_pct", 0.0)),
        trail_atr=float(d["trail_atr"]), max_hold_bars=int(d["max_hold_bars"]),
        direction=d["direction"], use_1h_filter=bool(d["use_1h_filter"]),
        trend_filter_1h=d.get("trend_filter_1h", "ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement", False)),
    )


def q_pnls(trades, n_q=4):
    if not trades: return [0.0] * n_q
    k = len(trades) // n_q
    if k == 0: return [sum(t["pnl"] for t in trades)] + [0.0] * (n_q - 1)
    out = []
    for i in range(n_q):
        lo = i * k; hi = (i + 1) * k if i < n_q - 1 else len(trades)
        out.append(sum(t["pnl"] for t in trades[lo:hi]))
    return out


def split_stats(trades, is_frac=0.7):
    if not trades:
        return {"is": cb.stats([]), "oos": cb.stats([])}
    cut = int(len(trades) * is_frac)
    return {"is": cb.stats(trades[:cut]), "oos": cb.stats(trades[cut:])}


def run_variant(arr, base_cfg, filter_variant, require_4h, lev):
    cfg = replace(base_cfg, trend_filter_1h=filter_variant, require_4h_agreement=require_4h)
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    q_pos = sum(1 for q in quarts if q > 0)
    return {
        "filter": filter_variant, "4h": require_4h,
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "wr": full.get("wr"),
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"],
        "oos_pf": split["oos"]["pf"],
        "is_pf": split["is"]["pf"],
        "quarts_pos": q_pos,
        "quartiles": quarts,
    }


def grade(v):
    """PASS if OOS PF>=1.0, n>=20, >=3/4 positive quartiles, total $>0."""
    if v["n"] < 20: return "small"
    if v["pnl"] <= 0: return "unprofitable"
    if v["oos_pf"] is None or v["oos_pf"] < 1.0: return "fails OOS"
    if v["quarts_pos"] < 3: return "quartiles unstable"
    return "PASS"


def main():
    deployed = load_all()
    print(f"[1/2] Fetching data for {len(LIVE_SYMBOLS)} symbols...")
    arrs = {}
    for sym in LIVE_SYMBOLS:
        if sym not in deployed:
            print(f"   {sym}: no deployed config, skip"); continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        if len(d15) < 500 or len(d1h) < 100:
            print(f"   {sym}: insufficient data, skip"); continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

    print(f"\n[2/2] Testing 12 combos per symbol (6 filters × 2 4h states)\n")

    all_results = {}
    for sym, arr in arrs.items():
        dep = deployed[sym]
        base_cfg = _cfg_from_deployed(dep)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15

        variants = []
        for fv in FILTER_VARIANTS:
            for req_4h in (False, True):
                v = run_variant(arr, base_cfg, fv, req_4h, lev)
                v["grade"] = grade(v)
                variants.append(v)

        # Current deployed combo stats
        cur_fv = dep.get("trend_filter_1h", "ema_cross")
        cur_4h = bool(dep.get("require_4h_agreement", False))
        cur_v = next((v for v in variants if v["filter"] == cur_fv and v["4h"] == cur_4h), None)

        # Best PASSING variant by $
        passing = [v for v in variants if v["grade"] == "PASS"]
        best = max(passing, key=lambda v: v["pnl"]) if passing else None

        all_results[sym] = {
            "current": {"filter": cur_fv, "4h": cur_4h, "stats": cur_v},
            "best_passing": best,
            "all_variants": variants,
        }

        print(f"\n=== {sym} ===")
        cur_pf_str = f"{cur_v['pf']:.2f}" if cur_v['pf'] else "—"
        cur_oos_str = f"{cur_v['oos_pf']:.2f}" if cur_v['oos_pf'] else "—"
        print(f"Deployed: {cur_fv} / 4h={cur_4h} "
              f"| n={cur_v['n']} PF={cur_pf_str} ${cur_v['pnl']:+.0f} "
              f"OOS_PF={cur_oos_str} Q+={cur_v['quarts_pos']} → {cur_v['grade']}")
        # Show top 5 by $
        top5 = sorted(variants, key=lambda v: v["pnl"], reverse=True)[:5]
        print(f"  {'FILTER':<14} {'4h':<4} {'n':>4} {'PF':>6} {'$':>8} {'OOS_PF':>7} {'Q+':>3}  grade")
        for v in top5:
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            marker = " ←BEST" if v is best else ""
            cur_marker = " (CURRENT)" if (v["filter"] == cur_fv and v["4h"] == cur_4h) else ""
            print(f"  {v['filter']:<14} {'✓' if v['4h'] else '-':<4} {v['n']:>4} "
                  f"{pf:>6} ${v['pnl']:>+6.0f} {oos:>7} {v['quarts_pos']:>3}  "
                  f"{v['grade']}{marker}{cur_marker}")

    # Summary table
    print(f"\n{'='*100}")
    print(f"  RECOMMENDATIONS — swap only where PASSING alternative beats current by > $50")
    print(f"{'='*100}")
    print(f"{'SYM':<10} {'CURRENT':<22} {'$CUR':>8} {'BEST':<22} {'$BEST':>8} {'Δ':>8} ACTION")
    print("-" * 100)
    swap_queue = []
    for sym, r in all_results.items():
        cur = r["current"]["stats"]
        best = r["best_passing"]
        cur_label = f"{r['current']['filter']}/{'4h' if r['current']['4h'] else 'no4h'}"
        if best is None:
            print(f"{sym:<10} {cur_label:<22} ${cur['pnl']:>+6.0f} "
                  f"{'(no passing variant)':<22} {'—':>8} {'—':>8} keep")
            continue
        best_label = f"{best['filter']}/{'4h' if best['4h'] else 'no4h'}"
        delta = best["pnl"] - cur["pnl"]
        is_current_best = (best["filter"] == r["current"]["filter"] and
                           best["4h"] == r["current"]["4h"])
        action = "KEEP" if is_current_best else ("SWAP" if delta > 50 else "keep (gain too small)")
        print(f"{sym:<10} {cur_label:<22} ${cur['pnl']:>+6.0f} "
              f"{best_label:<22} ${best['pnl']:>+6.0f} ${delta:>+6.0f} {action}")
        if action == "SWAP":
            swap_queue.append((sym, r["current"], best))

    print(f"\nSwap candidates: {[s[0] for s in swap_queue]}")
    with open("/tmp/full_filter_grid.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"Full → /tmp/full_filter_grid.json")


if __name__ == "__main__":
    main()
