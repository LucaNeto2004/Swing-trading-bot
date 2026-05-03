"""TP grid on the 4 no-TP pullback_in_regime symbols (SOL, HYPE, ZEC, LINK).

These currently run with zero TPs — 100% of position rides until either
a validated opposing pivot, regime flip, the 3% flat SL, or max_hold.
Same MFE-giveback risk as ARB, but worse: no TP1 to book anything.

Unlike ensemble_hybrid, `pullback_exit` respects `sl_hit` (commod_backtest
line 418), so trail actually fires here — trail variants are meaningful.

Variants:
  BASELINE         no TPs, no trail (current)
  TP1_LIGHT        TP1 2.0 ATR × 30%
  TP1_HEAVY        TP1 2.0 ATR × 50% (book half)
  TP1_TRAIL        TP1 2.0 ATR × 30% + trail 1.5 ATR
  TP1_TP2          TP1 2.0×30% + TP2 3.5×30%
  FULL_LADDER      TP1 2.0×30% + TP2 3.5×30% + TP3 5.0×20%
  TRAIL_ONLY       no TPs + trail 1.5 ATR

Writes /tmp/tp_ladder_on_pullback.json.
"""
from __future__ import annotations

import os, sys, json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb
from research.ensemble_regime_test import (
    bootstrap, q_pnls, split_stats, grade,
    P_WIN_MIN, P_PF1_MIN, N_MIN,
    _cfg_from_deployed, _patch_weekday,
)

SYMBOLS = ["SOL", "HYPE", "ZEC", "LINK"]

VARIANTS = [
    # (label, tp1_atr, tp1_pct, tp2_atr, tp2_pct, tp3_atr, tp3_pct, trail_atr)
    ("BASELINE",     0.0, 0.0,   0.0, 0.0,   0.0, 0.0,  0.0),
    ("TP1_LIGHT",    2.0, 0.30,  0.0, 0.0,   0.0, 0.0,  0.0),
    ("TP1_HEAVY",    2.0, 0.50,  0.0, 0.0,   0.0, 0.0,  0.0),
    ("TP1_TRAIL",    2.0, 0.30,  0.0, 0.0,   0.0, 0.0,  1.5),
    ("TP1_TP2",      2.0, 0.30,  3.5, 0.30,  0.0, 0.0,  0.0),
    ("FULL_LADDER",  2.0, 0.30,  3.5, 0.30,  5.0, 0.20, 0.0),
    ("TRAIL_ONLY",   0.0, 0.0,   0.0, 0.0,   0.0, 0.0,  1.5),
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
    return {
        "label": label,
        "cfg": {"tp1": (tp1a, tp1p), "tp2": (tp2a, tp2p),
                "tp3": (tp3a, tp3p), "trail": trail},
        "n": full["n"], "pnl": full["pnl"], "pf": full["pf"],
        "wr": full["wr"], "dd": full["dd"],
        "is_pnl": split["is"]["pnl"], "is_pf": split["is"]["pf"], "is_n": split["is"]["n"],
        "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"], "oos_n": split["oos"]["n"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
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
            print(f"   {sym:<10} insufficient (n={len(d15)})"); continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        dep = dep_all[sym]
        print(f"   {sym:<10} 15m={len(d15)} ({days}d) dir={dep.get('direction')}")
        arrs[sym] = arr

        base = _cfg_from_deployed(dep)
        base = replace(base, max_hold_bars=1000)
        bases[sym] = base

    print(f"\n[2/2] Grid ({len(VARIANTS)} variants each)\n")

    all_results = {}
    for sym in arrs:
        arr = arrs[sym]; base = bases[sym]
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        print(f"\n=== {sym} (entry={base.entry_type}, exit={base.exit_type}, "
              f"dir={base.direction}, lev={INSTRUMENTS[sym].hl_max_leverage}×) ===")
        print(f"  {'VARIANT':<16} {'n':>3} {'PF':>5} {'$':>7} {'dd':>5} "
              f"{'IS$':>6} {'OOS$':>6} {'OOSpf':>6} {'Q+':>3}  "
              f"{'P(win)':>6} {'P(PF>1)':>7}  grade")
        variants = []
        for args in VARIANTS:
            v = run_variant(arr, base, lev, *args)
            v["grade"] = grade(v)
            variants.append(v)
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {v['label']:<16} {v['n']:>3} {pf:>5} ${v['pnl']:>+5.0f} {v['dd']:>5.1f} "
                  f"${v['is_pnl']:>+4.0f} ${v['oos_pnl']:>+4.0f} {oos:>6} "
                  f"{v['quarts_pos']:>3}  {v['p_win']:>6.2f} {v['p_pf1']:>7.2f}  {v['grade']}")
        all_results[sym] = variants

    print(f"\n{'='*110}")
    print(f"  DELTA vs BASELINE")
    print(f"{'='*110}")
    print(f"{'SYM':<8} {'VARIANT':<16} {'Δ $':>7} {'Δ OOS':>7} {'Δ PF':>7} {'Δ P(win)':>9} {'grade':<20}")
    picks = {}
    for sym, vs in all_results.items():
        baseline = next(v for v in vs if v["label"] == "BASELINE")
        for v in vs:
            if v["label"] == "BASELINE": continue
            d_pnl = v["pnl"] - baseline["pnl"]
            d_oos = v["oos_pnl"] - baseline["oos_pnl"]
            d_pf  = (v["pf"] or 0) - (baseline["pf"] or 0)
            d_pw  = v["p_win"] - baseline["p_win"]
            print(f"{sym:<8} {v['label']:<16} ${d_pnl:>+5.0f} ${d_oos:>+5.0f} "
                  f"{d_pf:>+7.2f} {d_pw:>+9.3f} {v['grade']:<20}")
        improving = [v for v in vs if v["label"] != "BASELINE"
                     and v["grade"] == "PASS"
                     and v["oos_pnl"] >= baseline["oos_pnl"]]
        if improving:
            best = max(improving, key=lambda v: (v["oos_pnl"], v["p_win"]))
            picks[sym] = best["label"]
            print(f"         → BEST: {best['label']}  (OOS +${best['oos_pnl']-baseline['oos_pnl']:.0f})")
        else:
            picks[sym] = None
            print(f"         → BASELINE still best")
        print()

    with open("/tmp/tp_ladder_on_pullback.json", "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "picks": picks,
        }, f, indent=2, default=str)
    print(f"Full → /tmp/tp_ladder_on_pullback.json")


if __name__ == "__main__":
    main()
