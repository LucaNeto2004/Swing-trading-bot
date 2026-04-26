"""Head-to-head comparison on the same 41d window:

  A. CURRENT — each live symbol runs its deployed config exactly as-is
  B. ENSEMBLE — every symbol (live + expansion) runs the best-passing
     ensemble_regime cell from a 3×2×2 grid (K×BOS×exit_type)

Shows: per-symbol $, OOS $, PF, P(win), and grand totals. Tells us whether
trading an ensemble universe beats the sharpshooter-8 universe we're on.

Writes /tmp/current_vs_ensemble.json.
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
from research.ensemble_regime_test import (
    bootstrap, q_pnls, split_stats, grade,
    K_OPTS, BOS_OPTS, EXIT_OPTS, N_BOOT, P_WIN_MIN, P_PF1_MIN, N_MIN,
)

LIVE = ["BTC", "HYPE", "SOL", "XRP", "kPEPE", "ENA", "ZEC", "xyz:CL"]
EXPANSION = [
    "AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "DOGE", "ETH", "FARTCOIN",
    "INJ", "LDO", "LINK", "LIT", "LTC", "NEAR", "ONDO", "OP", "PENDLE",
    "SEI", "SUI", "TAO", "TIA", "WLD",
]
SYM_LEV_EXP = {
    "ETH": 25, "DOGE": 10, "AVAX": 10, "LINK": 10, "SUI": 10, "NEAR": 10,
    "SEI": 10, "LDO": 10, "ADA": 10, "APT": 10, "ATOM": 10, "ARB": 10,
    "OP": 10, "INJ": 10, "AAVE": 10, "LTC": 10, "PENDLE": 10, "ONDO": 10,
    "TIA": 10, "TAO": 10, "WLD": 10, "FARTCOIN": 10, "LIT": 5,
}


def _patch_weekday(arr, sym):
    if not sym.startswith("xyz:"):
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
        exit_type=d.get("exit_type", "standard"),
    )


def default_cfg() -> cb.Cfg:
    return cb.Cfg(
        trend_filter="ema_slope", entry_type="ensemble_regime",
        rsi_oversold=30.0, rsi_overbought=70.0,
        sl_atr=2.0, tp1_atr=0.0, tp1_pct=0.0,
        tp2_atr=0.0, tp2_pct=0.0, tp3_atr=0.0, tp3_pct=0.0,
        trail_atr=0.0, max_hold_bars=1000,
        direction="both", use_1h_filter=False,
        trend_filter_1h="ema_cross", require_4h_agreement=False,
        exit_type="ensemble_hybrid", ensemble_k=4, require_bos_confirm=False,
    )


def summarize(trades):
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    boot = bootstrap(trades)
    return {
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "dd": full["dd"],
        "wr": full["wr"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        **boot,
    }


def run_ensemble_grid(arr, base, lev):
    """Test all 12 cells. Return best-PASS-by-(P(win)*OOS$), else best-near-miss."""
    best = None
    all_cells = []
    for K, bos, ex in product(K_OPTS, BOS_OPTS, EXIT_OPTS):
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
        s = summarize(trades)
        s["K"] = K; s["bos"] = bos; s["exit"] = ex
        s["grade"] = grade(s)
        all_cells.append(s)
    passing = [c for c in all_cells if c["grade"] == "PASS"]
    if passing:
        return max(passing, key=lambda c: c["p_win"] * max(c["oos_pnl"], 0)), "PASS", all_cells
    # near-miss fallback: relaxed to P(win)≥0.70, n≥20, OOS+, 3/4 Q+
    near = [c for c in all_cells if c["n"] >= 20 and c["oos_pnl"] > 0
            and c["p_win"] >= 0.70 and c["quarts_pos"] >= 3]
    if near:
        return max(near, key=lambda c: c["p_win"] * c["oos_pnl"]), "NEAR", all_cells
    # else best by OOS $
    if all_cells:
        return max(all_cells, key=lambda c: c["oos_pnl"]), "FAIL", all_cells
    return None, "NONE", all_cells


def main():
    dep_all = load_all()

    print(f"[1/3] Loading data for LIVE ({len(LIVE)}) + EXPANSION ({len(EXPANSION)})...")
    all_arrs = {}; lev_map = {}
    for sym in LIVE:
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        all_arrs[sym] = arr
        lev_map[sym] = INSTRUMENTS[sym].hl_max_leverage * 0.15
    for sym in EXPANSION:
        try:
            d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
            d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
            d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
            if len(d15) < 500: continue
            arr = cb.precompute(d15, d1h, d4h)
            _patch_weekday(arr, sym)
            all_arrs[sym] = arr
            lev_map[sym] = SYM_LEV_EXP.get(sym, 10) * 0.15
        except Exception as e:
            print(f"   {sym:<10} skip: {e}")
    print(f"   total loaded: {len(all_arrs)}")

    # -------- A: CURRENT DEPLOYED --------
    print(f"\n[2/3] A) CURRENT DEPLOYED on {len(LIVE)} live symbols")
    print(f"  {'SYM':<10} {'ENTRY':<16} {'EXIT':<14} {'n':>3} {'PF':>5} {'$':>7} "
          f"{'OOS $':>7} {'P(win)':>7} {'Q+':>3}")
    current_results = {}
    for sym in LIVE:
        dep = dep_all.get(sym)
        if not dep: continue
        cfg = _cfg_from_deployed(dep)
        trades = cb.backtest(all_arrs[sym], cfg, lev_map[sym])
        s = summarize(trades)
        s["entry_type"] = cfg.entry_type; s["exit_type"] = cfg.exit_type
        s["filter_1h"] = cfg.trend_filter_1h; s["req_4h"] = cfg.require_4h_agreement
        current_results[sym] = s
        pf = f"{s['pf']:.2f}" if s['pf'] else "—"
        print(f"  {sym:<10} {cfg.entry_type:<16} {cfg.exit_type:<14} "
              f"{s['n']:>3} {pf:>5} ${s['pnl']:>+5.0f} ${s['oos_pnl']:>+5.0f} "
              f"{s['p_win']:>7.2f} {s['quarts_pos']:>3}")

    a_total_n = sum(r["n"] for r in current_results.values())
    a_total_pnl = sum(r["pnl"] for r in current_results.values())
    a_total_oos = sum(r["oos_pnl"] for r in current_results.values())
    print(f"  {'TOTAL':<10} {'—':<16} {'—':<14} {a_total_n:>3} {'':>5} "
          f"${a_total_pnl:>+5.0f} ${a_total_oos:>+5.0f}")

    # -------- B: ENSEMBLE --------
    print(f"\n[3/3] B) ENSEMBLE (quant+BOS+regime) on ALL {len(all_arrs)} symbols")
    print(f"  {'SYM':<10} {'CELL':<28} {'n':>3} {'PF':>5} {'$':>7} {'OOS $':>7} "
          f"{'P(win)':>7} {'Q+':>3} {'STATUS':<6}")
    ens_results = {}
    base = default_cfg()
    for sym in all_arrs:
        arr = all_arrs[sym]
        # For LIVE symbols use their deployed base (keeps direction=long_only etc)
        if sym in dep_all:
            dep_base = _cfg_from_deployed(dep_all[sym])
        else:
            dep_base = base
        best, status, all_cells = run_ensemble_grid(arr, dep_base, lev_map[sym])
        if best is None: continue
        best_copy = dict(best)
        best_copy["status"] = status
        ens_results[sym] = best_copy
        label = f"K={best['K']}/bos={'T' if best['bos'] else 'F'}/{best['exit'].replace('ensemble_','')}"
        pf = f"{best['pf']:.2f}" if best['pf'] else "—"
        print(f"  {sym:<10} {label:<28} {best['n']:>3} {pf:>5} "
              f"${best['pnl']:>+5.0f} ${best['oos_pnl']:>+5.0f} "
              f"{best['p_win']:>7.2f} {best['quarts_pos']:>3} {status:<6}")

    # Subset scoring: strict-pass only
    pass_only = {s: r for s, r in ens_results.items() if r["status"] == "PASS"}
    near_only = {s: r for s, r in ens_results.items() if r["status"] == "NEAR"}

    b_pass_n = sum(r["n"] for r in pass_only.values())
    b_pass_pnl = sum(r["pnl"] for r in pass_only.values())
    b_pass_oos = sum(r["oos_pnl"] for r in pass_only.values())

    b_near_n = sum(r["n"] for r in near_only.values())
    b_near_pnl = sum(r["pnl"] for r in near_only.values())
    b_near_oos = sum(r["oos_pnl"] for r in near_only.values())

    # -------- COMPARISON --------
    print(f"\n{'='*100}")
    print(f"  GRAND TOTALS — 41d window, same data")
    print(f"{'='*100}")
    print(f"  {'BUCKET':<45} {'n syms':>7} {'n tr':>6} {'$':>9} {'OOS $':>9}")
    print(f"  {'A: CURRENT deployed (8 live)':<45} {len(current_results):>7} "
          f"{a_total_n:>6} ${a_total_pnl:>+7.0f} ${a_total_oos:>+7.0f}")
    print(f"  {'B: ENSEMBLE strict-PASS only':<45} {len(pass_only):>7} "
          f"{b_pass_n:>6} ${b_pass_pnl:>+7.0f} ${b_pass_oos:>+7.0f}")
    print(f"  {'B: ENSEMBLE PASS + NEAR-miss':<45} {len(pass_only)+len(near_only):>7} "
          f"{b_pass_n+b_near_n:>6} ${b_pass_pnl+b_near_pnl:>+7.0f} "
          f"${b_pass_oos+b_near_oos:>+7.0f}")
    print()
    print(f"  Δ PASS-only vs CURRENT:    ${b_pass_pnl - a_total_pnl:>+7.0f} total, "
          f"${b_pass_oos - a_total_oos:>+7.0f} OOS")
    print(f"  Δ PASS+NEAR vs CURRENT:    ${b_pass_pnl+b_near_pnl - a_total_pnl:>+7.0f} total, "
          f"${b_pass_oos+b_near_oos - a_total_oos:>+7.0f} OOS")

    # Per-symbol head-to-head on LIVE symbols
    print(f"\n  Per-symbol on LIVE (current vs best ensemble cell):")
    print(f"  {'SYM':<10} {'CUR $':>8} {'CUR OOS':>8} {'ENS $':>8} {'ENS OOS':>8} "
          f"{'Δ $':>8} {'Δ OOS':>8}  winner")
    for sym in LIVE:
        if sym not in current_results or sym not in ens_results: continue
        c = current_results[sym]; e = ens_results[sym]
        dpnl = e["pnl"] - c["pnl"]; doos = e["oos_pnl"] - c["oos_pnl"]
        winner = "ENSEMBLE" if (e["oos_pnl"] > c["oos_pnl"] and e["status"] == "PASS") else "CURRENT"
        print(f"  {sym:<10} ${c['pnl']:>+6.0f} ${c['oos_pnl']:>+6.0f} "
              f"${e['pnl']:>+6.0f} ${e['oos_pnl']:>+6.0f} "
              f"${dpnl:>+6.0f} ${doos:>+6.0f}  {winner}")

    with open("/tmp/current_vs_ensemble.json", "w") as f:
        json.dump({
            "current": current_results,
            "ensemble": ens_results,
            "totals": {
                "A_current":  {"n_sym": len(current_results), "n_tr": a_total_n,
                               "pnl": a_total_pnl, "oos_pnl": a_total_oos},
                "B_pass":     {"n_sym": len(pass_only), "n_tr": b_pass_n,
                               "pnl": b_pass_pnl, "oos_pnl": b_pass_oos},
                "B_passnear": {"n_sym": len(pass_only)+len(near_only),
                               "n_tr": b_pass_n+b_near_n,
                               "pnl": b_pass_pnl+b_near_pnl,
                               "oos_pnl": b_pass_oos+b_near_oos},
            },
        }, f, indent=2, default=str)
    print(f"\nFull → /tmp/current_vs_ensemble.json")


if __name__ == "__main__":
    main()
