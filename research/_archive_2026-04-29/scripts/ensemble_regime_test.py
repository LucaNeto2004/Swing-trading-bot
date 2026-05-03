"""Ensemble-regime strategy test with probability gates.

Combines all 5 quant filters (ema_cross, structure, hma_slope, sjm, kalman) as
votes. Enters on consensus transition (prev_count < K, cur_count >= K). Exits
when consensus drops below K-1.

Grid per symbol:
  K ∈ {3, 4, 5}                   — 3/5 moderate, 4/5 strong, 5/5 unanimous
  require_bos_confirm ∈ {F, T}    — with / without BOS gate
  exit_type ∈ {ensemble_regime, ensemble_hybrid}
                                  — pure ensemble exit OR TP1 partial + ride

= 3 × 2 × 2 = 12 cells per symbol × 8 symbols = 96 backtests.

Bootstrap 2000x for P(win), P(PF>1), 95% CI. Scorecard:
  n ≥ 20, P(win) ≥ 0.85, P(PF>1) ≥ 0.75, ≥3/4 quartiles positive, $ > 0.

Writes /tmp/ensemble_regime.json.
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


SYMBOLS = ["BTC", "HYPE", "SOL", "XRP", "kPEPE", "ENA", "ZEC", "xyz:CL"]
K_OPTS   = [3, 4, 5]
BOS_OPTS = [False, True]
EXIT_OPTS = ["ensemble_regime", "ensemble_hybrid"]

N_BOOT = 2000
SEED = 42

P_WIN_MIN = 0.85
P_PF1_MIN = 0.75
N_MIN = 20


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
        direction=d["direction"], use_1h_filter=False,  # ensemble replaces single filter
        trend_filter_1h="ema_cross",  # unused when entry_type=ensemble_regime
        require_4h_agreement=False,
        exit_type=d.get("exit_type", "standard"),
    )


def bootstrap(trades, n=N_BOOT, seed=SEED):
    if not trades:
        return {"p_win": 0.0, "p_pf1": 0.0, "pnl_lo": 0.0, "pnl_hi": 0.0, "pf_median": None}
    pnls = np.array([t["pnl"] for t in trades])
    rng = np.random.default_rng(seed)
    N = len(pnls)
    sums = np.zeros(n); pfs = np.zeros(n)
    for i in range(n):
        idx = rng.integers(0, N, N)
        sample = pnls[idx]
        sums[i] = sample.sum()
        wins = sample[sample > 0].sum()
        losses = abs(sample[sample <= 0].sum())
        pfs[i] = wins / losses if losses > 0 else np.nan
    pfs_clean = pfs[~np.isnan(pfs)]
    return {
        "p_win":     float((sums > 0).mean()),
        "p_pf1":     float((pfs_clean > 1).mean()) if len(pfs_clean) else 0.0,
        "pnl_lo":    float(np.quantile(sums, 0.025)),
        "pnl_hi":    float(np.quantile(sums, 0.975)),
        "pf_median": float(np.nanmedian(pfs)) if len(pfs_clean) else None,
    }


def q_pnls(trades, n_q=4):
    if not trades: return [0.0] * n_q
    k = len(trades) // n_q
    if k == 0: return [sum(t["pnl"] for t in trades)] + [0.0]*(n_q-1)
    out = []
    for i in range(n_q):
        lo = i*k; hi = (i+1)*k if i < n_q-1 else len(trades)
        out.append(sum(t["pnl"] for t in trades[lo:hi]))
    return out


def split_stats(trades, is_frac=0.7):
    if not trades:
        return {"is": cb.stats([]), "oos": cb.stats([])}
    cut = int(len(trades) * is_frac)
    return {"is": cb.stats(trades[:cut]), "oos": cb.stats(trades[cut:])}


def run(arr, base, K, bos, exit_type, lev):
    # ensemble_hybrid uses TP1 partial; pure ensemble uses no TPs
    if exit_type == "ensemble_hybrid":
        tp1_atr, tp1_pct = 2.0, 0.3
    else:
        tp1_atr, tp1_pct = 0.0, 0.0
    cfg = replace(base,
                  entry_type="ensemble_regime", exit_type=exit_type,
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
        "K": K, "bos": bos, "exit": exit_type,
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "dd": full["dd"], "wr": full["wr"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "is_n":  split["is"]["n"],  "is_pnl":  split["is"]["pnl"],  "is_pf":  split["is"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
        **boot,
    }


def grade(v):
    if v["n"] < N_MIN:             return "small"
    if v["pnl"] <= 0:              return "unprofitable"
    if v["p_win"] < P_WIN_MIN:     return f"P(win)={v['p_win']:.2f}<{P_WIN_MIN}"
    if v["p_pf1"] < P_PF1_MIN:     return f"P(PF>1)={v['p_pf1']:.2f}<{P_PF1_MIN}"
    if v["quarts_pos"] < 3:        return "quartiles unstable"
    return "PASS"


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
        if len(d15) < 500: continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr; bases[sym] = _cfg_from_deployed(dep_all[sym])

    print(f"\n[2/2] Ensemble-regime grid ({len(K_OPTS)}×{len(BOS_OPTS)}×{len(EXIT_OPTS)} per sym, "
          f"P(win)≥{P_WIN_MIN}, P(PF>1)≥{P_PF1_MIN}, N_boot={N_BOOT})\n")

    all_results = {}
    for sym in arrs:
        arr = arrs[sym]; base = bases[sym]
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        print(f"\n=== {sym} ===")
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

    # Summary
    print(f"\n{'='*130}")
    print(f"  TOP PASSING ENSEMBLE CELL PER SYMBOL (ranked by P(win) × OOS $)")
    print(f"{'='*130}")
    print(f"{'SYM':<10} {'CELL':<28} {'n':>3} {'PF':>5} {'$':>7} {'OOS $':>7} "
          f"{'P(win)':>7} {'P(PF>1)':>8} {'$CI_lo':>7} {'$CI_hi':>7}")
    ship = {}
    for sym, vs in all_results.items():
        passing = [v for v in vs if v["grade"] == "PASS"]
        if not passing:
            print(f"{sym:<10} (no PASS)"); continue
        best = max(passing, key=lambda v: v["p_win"] * max(v["oos_pnl"], 0))
        label = f"K={best['K']}/bos={'T' if best['bos'] else 'F'}/{best['exit']}"
        print(f"{sym:<10} {label:<28} {best['n']:>3} {best['pf']:>5.2f} "
              f"${best['pnl']:>+5.0f} ${best['oos_pnl']:>+5.0f} "
              f"{best['p_win']:>7.2f} {best['p_pf1']:>8.2f} "
              f"${best['pnl_lo']:>+5.0f} ${best['pnl_hi']:>+5.0f}")
        ship[sym] = {"K": best["K"], "bos": best["bos"], "exit": best["exit"],
                     "pnl": best["pnl"], "oos_pnl": best["oos_pnl"],
                     "p_win": best["p_win"], "p_pf1": best["p_pf1"]}

    out = "/tmp/ensemble_regime.json"
    with open(out, "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "ship_candidates": ship,
            "scorecard": {"p_win_min": P_WIN_MIN, "p_pf1_min": P_PF1_MIN,
                          "n_min": N_MIN, "q_pos_min": 3},
        }, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
