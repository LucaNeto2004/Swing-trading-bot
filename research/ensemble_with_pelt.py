"""Ensemble-regime with PELT as a 6th filter vote.

Same grid as ensemble_regime_test.py but precomputes PELT up/dn arrays and
injects them into arr['up_pelt'] / arr['dn_pelt']. The ensemble backtest
logic automatically uses 6 filters when those keys are present.

Compares PASS set vs the 5-filter result. If PELT adds confidence or unlocks
more symbols, ship it.

Writes /tmp/ensemble_with_pelt.json.
"""
from __future__ import annotations

import os, sys, json
from dataclasses import replace
from itertools import product

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb
from research.pelt_signal import pelt_lookup_1h
from research.ensemble_regime_test import (
    bootstrap, q_pnls, split_stats, grade,
    K_OPTS, BOS_OPTS, EXIT_OPTS, N_BOOT, P_WIN_MIN, P_PF1_MIN, N_MIN,
)
from research.current_vs_ensemble import (
    LIVE, EXPANSION, SYM_LEV_EXP, _patch_weekday, _cfg_from_deployed,
    default_cfg, summarize,
)

# With 6 filters total, K values are now 3-6 (3/6 moderate, 4/6 majority,
# 5/6 strong, 6/6 unanimous).
K_OPTS_6 = [4, 5, 6]


def run(arr, base, K, bos, ex, lev):
    if ex == "ensemble_hybrid":
        tp1_atr, tp1_pct = 2.0, 0.3
    else:
        tp1_atr, tp1_pct = 0.0, 0.0
    cfg = replace(base,
                  entry_type="ensemble_regime", exit_type=ex,
                  ensemble_k=K, require_bos_confirm=bos,
                  tp1_atr=tp1_atr, tp1_pct=tp1_pct,
                  tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                  max_hold_bars=1000)
    trades = cb.backtest(arr, cfg, lev)
    full  = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    boot = bootstrap(trades)
    return {
        "K": K, "bos": bos, "exit": ex,
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "dd": full["dd"], "wr": full["wr"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "is_n":  split["is"]["n"],  "is_pnl":  split["is"]["pnl"],  "is_pf":  split["is"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
        **boot,
    }


def main():
    dep_all = load_all()
    print(f"[1/3] Loading data + PELT for LIVE({len(LIVE)}) + EXPANSION({len(EXPANSION)})...")
    arrs = {}; lev_map = {}; bases = {}
    for sym in LIVE + EXPANSION:
        try:
            d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
            d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
            d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
            if len(d15) < 500: continue
            arr = cb.precompute(d15, d1h, d4h)
            _patch_weekday(arr, sym)
            # PELT as the 6th vote
            up_p, dn_p = pelt_lookup_1h(d15, d1h)
            arr["up_pelt"] = up_p
            arr["dn_pelt"] = dn_p
            arrs[sym] = arr
            lev_map[sym] = (INSTRUMENTS[sym].hl_max_leverage if sym in LIVE else
                            SYM_LEV_EXP.get(sym, 10)) * 0.15
            bases[sym] = _cfg_from_deployed(dep_all[sym]) if sym in dep_all else default_cfg()
            pelt_density = (up_p.sum() + dn_p.sum()) / max(len(up_p), 1)
            print(f"   {sym:<10} 15m={len(d15)}  PELT density={pelt_density*100:.0f}%")
        except Exception as e:
            print(f"   {sym:<10} skip: {e}")

    print(f"\n[2/3] 6-filter ensemble grid ({len(K_OPTS_6)}×{len(BOS_OPTS)}×{len(EXIT_OPTS)} per sym)\n")

    all_results = {}
    for sym in arrs:
        arr = arrs[sym]; base = bases[sym]; lev = lev_map[sym]
        print(f"\n=== {sym} ===")
        print(f"  {'K':>1} {'BOS':<4} {'EXIT':<16} {'n':>3} {'PF':>5} {'$':>7} {'dd':>5} "
              f"{'OOS$':>6} {'OOSpf':>6} {'Q+':>3}  {'P(win)':>6} {'P(PF>1)':>7}  grade")
        variants = []
        for K, bos, ex in product(K_OPTS_6, BOS_OPTS, EXIT_OPTS):
            v = run(arr, base, K, bos, ex, lev)
            v["grade"] = grade(v)
            variants.append(v)
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {K:>1} {str(bos)[:4]:<4} {ex:<16} {v['n']:>3} {pf:>5} "
                  f"${v['pnl']:>+5.0f} {v['dd']:>5.1f} ${v['oos_pnl']:>+4.0f} {oos:>6} "
                  f"{v['quarts_pos']:>3}  {v['p_win']:>6.2f} {v['p_pf1']:>7.2f}  {v['grade']}")
        all_results[sym] = variants

    # Best-PASS summary
    print(f"\n{'='*130}")
    print(f"  6-FILTER (with PELT) — PASSING cells (P(win)≥{P_WIN_MIN}, P(PF>1)≥{P_PF1_MIN}, n≥{N_MIN}, 3/4 Q+, $>0)")
    print(f"{'='*130}")
    print(f"{'SYM':<10} {'CELL':<30} {'n':>3} {'PF':>5} {'$':>7} {'OOS $':>7} "
          f"{'P(win)':>7} {'P(PF>1)':>8} {'$CI_lo':>7} {'$CI_hi':>7}")
    ship = {}
    for sym, vs in all_results.items():
        passing = [v for v in vs if v["grade"] == "PASS"]
        if not passing: continue
        best = max(passing, key=lambda v: v["p_win"] * max(v["oos_pnl"], 0))
        label = f"K={best['K']}/6 bos={'T' if best['bos'] else 'F'}/{best['exit'].replace('ensemble_','')}"
        print(f"{sym:<10} {label:<30} {best['n']:>3} {best['pf']:>5.2f} "
              f"${best['pnl']:>+5.0f} ${best['oos_pnl']:>+5.0f} "
              f"{best['p_win']:>7.2f} {best['p_pf1']:>8.2f} "
              f"${best['pnl_lo']:>+5.0f} ${best['pnl_hi']:>+5.0f}")
        ship[sym] = {"K": best["K"], "bos": best["bos"], "exit": best["exit"],
                     "pnl": best["pnl"], "oos_pnl": best["oos_pnl"],
                     "p_win": best["p_win"], "p_pf1": best["p_pf1"],
                     "n": best["n"], "pf": best["pf"]}

    # Compare to 5-filter (loaded from last run if available)
    print(f"\n[3/3] 5-filter vs 6-filter (with PELT) comparison")
    try:
        old = json.load(open("/tmp/ensemble_regime.json"))
        old_ship = old["ship_candidates"]
    except Exception:
        old_ship = {}
    print(f"  {'SYM':<10} {'5-filter':<30} {'6-filter (+PELT)':<30} {'Δ OOS $':>10}")
    all_syms = sorted(set(list(ship.keys()) + list(old_ship.keys())))
    for sym in all_syms:
        o = old_ship.get(sym, {}); n = ship.get(sym, {})
        o_label = (f"K={o['K']}/bos={'T' if o.get('bos') else 'F'}/{o.get('exit','').replace('ensemble_','')}"
                   if o else "(no pass)")
        n_label = (f"K={n['K']}/6 bos={'T' if n.get('bos') else 'F'}/{n.get('exit','').replace('ensemble_','')}"
                   if n else "(no pass)")
        delta = (n.get("oos_pnl", 0) - o.get("oos_pnl", 0)) if (o and n) else None
        delta_s = f"${delta:>+6.0f}" if delta is not None else "    —"
        print(f"  {sym:<10} {o_label:<30} {n_label:<30} {delta_s:>10}")

    out = "/tmp/ensemble_with_pelt.json"
    with open(out, "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "ship_candidates": ship,
        }, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
