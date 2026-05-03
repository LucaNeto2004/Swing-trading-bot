"""Ensemble-regime test across the expansion universe (symbols fetched into
the cache by the parallel agent — 23 symbols beyond the 8 currently live).

Uses a default Cfg (not a deployed one) because these symbols aren't
configured yet. Runs the same 3×2×2 grid per symbol.

Writes /tmp/ensemble_expansion.json.
"""
from __future__ import annotations

import os, sys, json
from dataclasses import replace
from itertools import product

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

import research.commod_backtest as cb
from research.ensemble_regime_test import (
    bootstrap, q_pnls, split_stats, grade,
    K_OPTS, BOS_OPTS, EXIT_OPTS, N_BOOT, SEED,
    P_WIN_MIN, P_PF1_MIN, N_MIN,
)


# Everything in the cache except the 8 already-live symbols. "xyz_CL" on disk
# maps to "xyz:CL" at the API level.
EXPANSION = [
    "AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "DOGE", "ETH", "FARTCOIN",
    "INJ", "LDO", "LINK", "LIT", "LTC", "NEAR", "ONDO", "OP", "PENDLE",
    "SEI", "SUI", "TAO", "TIA", "WLD",
]

# Default leverage per symbol. HL caps most crypto perps at 10-25×; use a
# conservative 10× unless we know better. Margin = 0.15 × lev gives 1.5×
# effective — same as the bot's fixed 15% margin × 10× set-lev for 58bro sizing.
DEFAULT_LEV = 10
SYM_LEV = {
    "ETH": 25, "DOGE": 10, "AVAX": 10, "LINK": 10, "SUI": 10, "NEAR": 10,
    "SEI": 10, "LDO": 10, "ADA": 10, "APT": 10, "ATOM": 10, "ARB": 10,
    "OP": 10, "INJ": 10, "AAVE": 10, "LTC": 10, "PENDLE": 10, "ONDO": 10,
    "TIA": 10, "TAO": 10, "WLD": 10, "FARTCOIN": 10, "LIT": 5,
}


def default_cfg() -> cb.Cfg:
    return cb.Cfg(
        trend_filter="ema_slope",
        entry_type="ensemble_regime",
        rsi_oversold=30.0, rsi_overbought=70.0,
        sl_atr=2.0, tp1_atr=0.0, tp1_pct=0.0,
        tp2_atr=0.0, tp2_pct=0.0, tp3_atr=0.0, tp3_pct=0.0,
        trail_atr=0.0, max_hold_bars=1000,
        direction="both", use_1h_filter=False,
        trend_filter_1h="ema_cross", require_4h_agreement=False,
        exit_type="ensemble_hybrid", ensemble_k=4, require_bos_confirm=False,
    )


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
    full = cb.stats(trades)
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
    print(f"[1/2] Loading cached data for {len(EXPANSION)} expansion symbols...")
    arrs = {}
    for sym in EXPANSION:
        try:
            d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
            d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
            d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
        except Exception as e:
            print(f"   {sym:<12} FETCH ERROR: {e}"); continue
        if len(d15) < 500:
            print(f"   {sym:<12} insufficient data (n={len(d15)})"); continue
        arr = cb.precompute(d15, d1h, d4h)
        if not sym.startswith("xyz:"):
            arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<12} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

    print(f"\n[2/2] Ensemble-regime grid on {len(arrs)} symbols\n")

    all_results = {}
    base = default_cfg()
    for sym in arrs:
        arr = arrs[sym]
        lev = SYM_LEV.get(sym, DEFAULT_LEV) * 0.15
        print(f"\n=== {sym} (lev={SYM_LEV.get(sym, DEFAULT_LEV)}× eff={lev:.2f}) ===")
        print(f"  {'K':>1} {'BOS':<4} {'EXIT':<16} {'n':>3} {'PF':>5} {'$':>7} {'dd':>5} "
              f"{'OOS$':>6} {'OOSpf':>6} {'Q+':>3}  {'P(win)':>6} {'P(PF>1)':>7} "
              f"{'$CI_lo':>7} {'$CI_hi':>7}  grade")
        variants = []
        for K, bos, ex in product(K_OPTS, BOS_OPTS, EXIT_OPTS):
            v = run(arr, base, K, bos, ex, lev)
            v["grade"] = grade(v)
            variants.append(v)
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {K:>1} {str(bos)[:4]:<4} {ex:<16} {v['n']:>3} {pf:>5} "
                  f"${v['pnl']:>+5.0f} {v['dd']:>5.1f} ${v['oos_pnl']:>+4.0f} {oos:>6} "
                  f"{v['quarts_pos']:>3}  {v['p_win']:>6.2f} {v['p_pf1']:>7.2f} "
                  f"${v['pnl_lo']:>+5.0f} ${v['pnl_hi']:>+5.0f}  {v['grade']}")
        all_results[sym] = variants

    # Ship summary
    print(f"\n{'='*135}")
    print(f"  EXPANSION — PASSING ENSEMBLE CELLS (P(win)≥{P_WIN_MIN}, P(PF>1)≥{P_PF1_MIN}, n≥{N_MIN}, 3/4 Q+, $>0)")
    print(f"{'='*135}")
    print(f"{'SYM':<10} {'CELL':<28} {'n':>3} {'PF':>5} {'$':>7} {'OOS $':>7} "
          f"{'P(win)':>7} {'P(PF>1)':>8} {'$CI_lo':>7} {'$CI_hi':>7}  Q+")
    ship = {}
    for sym, vs in all_results.items():
        passing = [v for v in vs if v["grade"] == "PASS"]
        if not passing:
            continue
        best = max(passing, key=lambda v: v["p_win"] * max(v["oos_pnl"], 0))
        label = f"K={best['K']}/bos={'T' if best['bos'] else 'F'}/{best['exit']}"
        print(f"{sym:<10} {label:<28} {best['n']:>3} {best['pf']:>5.2f} "
              f"${best['pnl']:>+5.0f} ${best['oos_pnl']:>+5.0f} "
              f"{best['p_win']:>7.2f} {best['p_pf1']:>8.2f} "
              f"${best['pnl_lo']:>+5.0f} ${best['pnl_hi']:>+5.0f}  {best['quarts_pos']}")
        ship[sym] = {
            "K": best["K"], "bos": best["bos"], "exit": best["exit"],
            "pnl": best["pnl"], "oos_pnl": best["oos_pnl"],
            "p_win": best["p_win"], "p_pf1": best["p_pf1"],
            "n": best["n"], "pf": best["pf"], "quarts_pos": best["quarts_pos"],
        }
    no_pass = [s for s in all_results if s not in ship]
    print(f"\nNo-pass: {', '.join(no_pass)}")

    out = "/tmp/ensemble_expansion.json"
    with open(out, "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "ship_candidates": ship,
        }, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
