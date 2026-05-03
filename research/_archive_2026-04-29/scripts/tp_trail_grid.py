"""TP2/TP3/trail grid on the 8 ARB-pattern ensemble_hybrid symbols.

These 8 symbols all currently run TP1-only (tp2=tp3=trail=0), so the
70% runner has exactly one exit path: 1h ensemble count < K-1. That
means large MFE-giveback is structurally unavoidable — the runner
must wait for filter decay before closing.

This script tests whether adding TP2/TP3 or an ATR trail improves the
realized $ / PF / OOS-stability without breaking the backtest's
profitability profile. Keeps the current deployed K + BOS + entry
settings per symbol; only varies the exit structure.

Variants per symbol:
  BASELINE          current deployed: TP1 2.0×30%, no tp2/tp3/trail
  TRAIL_1.5         TP1 2.0×30% + trail 1.5 ATR
  TRAIL_1.0         TP1 2.0×30% + trail 1.0 ATR  (tighter)
  TP2_4             TP1 2.0×30% + TP2 4.0 ATR × 30%
  TP2_TP3           TP1 2.0×30% + TP2 3.5 ATR × 30% + TP3 5.0 ATR × 20%
  TP2_TP3_TRAIL     TP1 2.0×30% + TP2 3.5 ATR × 30% + TP3 5.0 ATR × 20% + trail 2.0
  LIGHT_TP1_TRAIL   TP1 2.0×10% + trail 1.0 ATR  (book less at TP1, trail more)

Scorecard (per ensemble_regime_test.py):
  n ≥ 20, P(win) ≥ 0.85, P(PF>1) ≥ 0.75, ≥3/4 quartiles positive, $ > 0.

Writes /tmp/tp_trail_grid.json.
"""
from __future__ import annotations

import os, sys, json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb
from research.ensemble_regime_test import (
    bootstrap, q_pnls, split_stats, grade,
    P_WIN_MIN, P_PF1_MIN, N_MIN,
    _cfg_from_deployed, _patch_weekday,
)

SYMBOLS = ["ARB", "BTC", "ENA", "ETH", "INJ", "OP", "PENDLE", "TIA"]

VARIANTS = [
    # (label, tp1_atr, tp1_pct, tp2_atr, tp2_pct, tp3_atr, tp3_pct, trail_atr)
    ("BASELINE",         2.0, 0.30,  0.0, 0.0,   0.0, 0.0,  0.0),
    ("TRAIL_1.5",        2.0, 0.30,  0.0, 0.0,   0.0, 0.0,  1.5),
    ("TRAIL_1.0",        2.0, 0.30,  0.0, 0.0,   0.0, 0.0,  1.0),
    ("TP2_4",            2.0, 0.30,  4.0, 0.30,  0.0, 0.0,  0.0),
    ("TP2_TP3",          2.0, 0.30,  3.5, 0.30,  5.0, 0.20, 0.0),
    ("TP2_TP3_TRAIL",    2.0, 0.30,  3.5, 0.30,  5.0, 0.20, 2.0),
    ("LIGHT_TP1_TRAIL",  2.0, 0.10,  0.0, 0.0,   0.0, 0.0,  1.0),
]


def run_variant(arr, base_cfg, lev, label, tp1a, tp1p, tp2a, tp2p, tp3a, tp3p, trail):
    cfg = replace(
        base_cfg,
        tp1_atr=tp1a, tp1_pct=tp1p,
        tp2_atr=tp2a, tp2_pct=tp2p,
        tp3_atr=tp3a, tp3_pct=tp3p,
        trail_atr=trail,
    )
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    boot = bootstrap(trades)

    # MFE giveback telemetry — avg % of max-favorable that was given back
    # by the time the trade closed. Requires trades to carry max_fav + final
    # exit price info (commod_backtest records `max_fav` in the position but
    # doesn't necessarily surface it on exit). Fall back to None if missing.
    givebacks = []
    for t in trades:
        mf = t.get("max_fav")
        pnl = t.get("pnl", 0.0)
        if mf is None or mf <= 0:
            continue
        # approximate giveback ratio: 1 - realized / peak (both in $ terms
        # relative to the position notional)
        notional = t.get("notional", 0.0) or 1.0
        realized_pct = pnl / notional
        peak_pct = mf  # already in fractional-of-notional units per backtester
        if peak_pct <= 0:
            continue
        givebacks.append(max(0.0, 1.0 - (realized_pct / peak_pct)))
    gb = float(np.median(givebacks)) if givebacks else None

    return {
        "label": label,
        "cfg": {
            "tp1": (tp1a, tp1p), "tp2": (tp2a, tp2p), "tp3": (tp3a, tp3p),
            "trail": trail,
        },
        "n": full["n"], "pnl": full["pnl"], "pf": full["pf"],
        "wr": full["wr"], "dd": full["dd"],
        "is_pnl": split["is"]["pnl"], "is_pf": split["is"]["pf"], "is_n": split["is"]["n"],
        "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"], "oos_n": split["oos"]["n"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
        "mfe_giveback_med": gb,
        **boot,
    }


def main():
    dep_all = load_all()
    print(f"[1/2] Fetching data for {len(SYMBOLS)} symbols...")
    arrs = {}; bases = {}
    for sym in SYMBOLS:
        if sym not in dep_all:
            print(f"   {sym:<10} NOT DEPLOYED — skip"); continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
        if len(d15) < 500:
            print(f"   {sym:<10} insufficient data (n={len(d15)})"); continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

        # Build base cfg from deployed, then force ensemble_regime/hybrid
        # identity — ensures tp/trail tweaks are the only axis changing.
        base = _cfg_from_deployed(dep_all[sym])
        base = replace(base,
                       entry_type="ensemble_regime",
                       exit_type="ensemble_hybrid",
                       ensemble_k=int(dep_all[sym].get("ensemble_k", 4)),
                       require_bos_confirm=bool(dep_all[sym].get("require_bos_confirm", False)),
                       max_hold_bars=1000)
        bases[sym] = base

    print(f"\n[2/2] Variant grid on {len(arrs)} symbols ({len(VARIANTS)} variants each)\n")

    all_results = {}
    for sym in arrs:
        arr = arrs[sym]; base = bases[sym]
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        print(f"\n=== {sym} (K={base.ensemble_k}, lev={INSTRUMENTS[sym].hl_max_leverage}×) ===")
        print(f"  {'VARIANT':<18} {'n':>3} {'PF':>5} {'$':>7} {'dd':>5} "
              f"{'IS$':>6} {'OOS$':>6} {'OOSpf':>6} {'Q+':>3}  "
              f"{'P(win)':>6} {'P(PF>1)':>7} {'GB%':>5}  grade")
        variants = []
        for args in VARIANTS:
            v = run_variant(arr, base, lev, *args)
            v["grade"] = grade(v)
            variants.append(v)
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            gb = f"{v['mfe_giveback_med']*100:>4.0f}" if v['mfe_giveback_med'] is not None else "  —"
            print(f"  {v['label']:<18} {v['n']:>3} {pf:>5} ${v['pnl']:>+5.0f} {v['dd']:>5.1f} "
                  f"${v['is_pnl']:>+4.0f} ${v['oos_pnl']:>+4.0f} {oos:>6} "
                  f"{v['quarts_pos']:>3}  {v['p_win']:>6.2f} {v['p_pf1']:>7.2f} {gb:>5}  {v['grade']}")
        all_results[sym] = variants

    # Cross-symbol summary — compare each variant vs baseline per symbol
    print(f"\n{'='*120}")
    print(f"  PER-SYMBOL DELTA vs BASELINE (all $ figures are change from current config)")
    print(f"{'='*120}")
    print(f"{'SYM':<8} {'VARIANT':<18} {'Δ $':>7} {'Δ OOS':>7} {'Δ PF':>7} {'Δ P(win)':>9} {'grade':<20}")
    best_per_sym = {}
    for sym, vs in all_results.items():
        baseline = next(v for v in vs if v["label"] == "BASELINE")
        for v in vs:
            if v["label"] == "BASELINE": continue
            d_pnl = v["pnl"] - baseline["pnl"]
            d_oos = v["oos_pnl"] - baseline["oos_pnl"]
            d_pf  = (v["pf"] or 0) - (baseline["pf"] or 0)
            d_pw  = v["p_win"] - baseline["p_win"]
            grade_str = v["grade"] if v["grade"] != "PASS" else "PASS"
            print(f"{sym:<8} {v['label']:<18} ${d_pnl:>+5.0f} ${d_oos:>+5.0f} {d_pf:>+7.2f} {d_pw:>+9.3f} {grade_str:<20}")
        # pick best improving variant: OOS-weighted
        improving = [v for v in vs if v["label"] != "BASELINE"
                     and v["grade"] == "PASS"
                     and v["oos_pnl"] >= baseline["oos_pnl"]]
        if improving:
            best = max(improving, key=lambda v: (v["oos_pnl"], v["p_win"]))
            best_per_sym[sym] = best["label"]
            print(f"         → BEST: {best['label']}  (OOS +${best['oos_pnl']-baseline['oos_pnl']:.0f})")
        else:
            best_per_sym[sym] = None
            print(f"         → BASELINE still best (no variant beat OOS $ + passed scorecard)")
        print()

    # Write everything
    out = "/tmp/tp_trail_grid.json"
    with open(out, "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "best_per_symbol": best_per_sym,
            "scorecard": {"p_win_min": P_WIN_MIN, "p_pf1_min": P_PF1_MIN,
                          "n_min": N_MIN, "q_pos_min": 3},
        }, f, indent=2, default=str)
    print(f"Full → {out}")


if __name__ == "__main__":
    main()
