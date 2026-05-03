"""Force BOS entry + quant filter across ALL live symbols, with bootstrap
probability gates.

For each live symbol (9 deployed):
  - entry_type = bos_structural  (force — ignore deployed entry)
  - exit_type  = bos_hybrid      (TP1 partial + structural tail)
  - grid: trend_filter_1h ∈ {sjm, hma_slope, kalman, both_agree} × require_4h ∈ {False, True}
  = 8 cells per symbol × 9 symbols = 72 backtests

Probability layer:
  - Bootstrap 2000x on trade PnLs: 95% CI on total $ and PF
  - p_win = fraction of resamples with $ > 0
  - p_pf_above_1 = fraction of resamples with PF > 1
  - Grade uses p_win ≥ 0.90 (strong probability of profit) as the gate
    instead of raw $>0 — reduces false positives from lucky-sample PF spikes.

Writes /tmp/bos_quant_prob.json.
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


SYMBOLS = ["BTC", "HYPE", "SOL", "XRP", "kPEPE", "ENA", "ZEC", "xyz:CL"]  # LIT disabled in settings.py
FILTERS = ["sjm", "hma_slope", "kalman", "both_agree"]
FOUR_H  = [False, True]
N_BOOT  = 2000
SEED    = 42


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


def bootstrap(trades, n=N_BOOT, seed=SEED):
    """Bootstrap PnL list. Returns dict with CI + probabilities."""
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
        "p_win":       float((sums > 0).mean()),           # prob total $ > 0
        "p_pf1":       float((pfs_clean > 1).mean()) if len(pfs_clean) else 0.0,
        "pnl_lo":      float(np.quantile(sums, 0.025)),    # 95% CI lower
        "pnl_hi":      float(np.quantile(sums, 0.975)),    # 95% CI upper
        "pf_median":   float(np.nanmedian(pfs)) if len(pfs_clean) else None,
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


def run(arr, base, f1h, r4h, lev):
    cfg = replace(base,
                  entry_type="bos_structural", exit_type="bos_hybrid",
                  tp1_atr=2.0, tp1_pct=0.3, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                  trend_filter_1h=f1h, require_4h_agreement=r4h)
    trades = cb.backtest(arr, cfg, lev)
    full  = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    boot = bootstrap(trades)
    return {
        "filter_1h": f1h, "req_4h": r4h,
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "dd": full["dd"], "wr": full["wr"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "is_n":  split["is"]["n"],  "is_pnl":  split["is"]["pnl"],  "is_pf":  split["is"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
        **boot,  # p_win, p_pf1, pnl_lo, pnl_hi, pf_median
    }


def grade(v):
    """Probability-gated scorecard."""
    if v["n"] < 20:                             return "small"
    if v["p_win"]  < 0.90:                      return f"P(win)={v['p_win']:.2f}<0.90"
    if v["p_pf1"]  < 0.80:                      return f"P(PF>1)={v['p_pf1']:.2f}<0.80"
    if v["quarts_pos"] < 3:                     return "quartiles unstable"
    if v["pnl"] <= 0:                           return "unprofitable"
    return "PASS"


def main():
    dep_all = load_all()
    print(f"[1/2] Fetching data for {len(SYMBOLS)} live symbols...")
    arrs = {}
    bases = {}
    for sym in SYMBOLS:
        if sym not in dep_all:
            print(f"   {sym:<10} NOT DEPLOYED — skip"); continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
        if len(d15) < 500:
            print(f"   {sym:<10} insufficient data"); continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)")
        arrs[sym]  = arr
        bases[sym] = _cfg_from_deployed(dep_all[sym])

    print(f"\n[2/2] BOS + quant grid w/ {N_BOOT}-boot probability gates\n")

    all_results = {}
    for sym in arrs:
        arr  = arrs[sym]
        base = bases[sym]
        lev  = INSTRUMENTS[sym].hl_max_leverage * 0.15

        print(f"\n=== {sym}  (forced entry=bos_structural exit=bos_hybrid) ===")
        print(f"  {'FILTER':<12} {'4h':<5} {'n':>3} {'PF':>5} {'$':>7} {'dd':>5} "
              f"{'OOS$':>6} {'OOSpf':>6} {'Q+':>3}  {'P(win)':>6} {'P(PF>1)':>7} "
              f"{'$CI_lo':>7} {'$CI_hi':>7}  grade")

        variants = []
        for f1h, r4h in product(FILTERS, FOUR_H):
            v = run(arr, base, f1h, r4h, lev)
            v["grade"] = grade(v)
            variants.append(v)
            pf  = f"{v['pf']:.2f}"  if v['pf']  else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {f1h:<12} {('ON' if r4h else 'off'):<5} "
                  f"{v['n']:>3} {pf:>5} ${v['pnl']:>+5.0f} {v['dd']:>5.1f} "
                  f"${v['oos_pnl']:>+4.0f} {oos:>6} {v['quarts_pos']:>3}  "
                  f"{v['p_win']:>6.2f} {v['p_pf1']:>7.2f} "
                  f"${v['pnl_lo']:>+5.0f} ${v['pnl_hi']:>+5.0f}  {v['grade']}")
        all_results[sym] = variants

    # Summary: best PASSING cell per symbol, ranked by P(win) × OOS $
    print(f"\n{'='*125}")
    print(f"  TOP PASSING BOS+QUANT CELL PER SYMBOL  (probability-gated; ranked by P(win) × OOS $)")
    print(f"{'='*125}")
    print(f"{'SYM':<10} {'BEST CELL':<22} {'n':>3} {'PF':>5} {'$':>7} {'OOS $':>7} "
          f"{'P(win)':>7} {'P(PF>1)':>8} {'$CI_lo':>7} {'$CI_hi':>7}")
    ship = {}
    for sym, vs in all_results.items():
        passing = [v for v in vs if v["grade"] == "PASS"]
        if not passing:
            print(f"{sym:<10} (no PASS)")
            continue
        best = max(passing, key=lambda v: v["p_win"] * max(v["oos_pnl"], 0))
        label = f"{best['filter_1h']}/{'4hON' if best['req_4h'] else '4hoff'}"
        print(f"{sym:<10} {label:<22} {best['n']:>3} {best['pf']:>5.2f} "
              f"${best['pnl']:>+5.0f} ${best['oos_pnl']:>+5.0f} "
              f"{best['p_win']:>7.2f} {best['p_pf1']:>8.2f} "
              f"${best['pnl_lo']:>+5.0f} ${best['pnl_hi']:>+5.0f}")
        ship[sym] = {
            "filter_1h": best["filter_1h"],
            "require_4h_agreement": best["req_4h"],
            "pnl": best["pnl"], "oos_pnl": best["oos_pnl"],
            "p_win": best["p_win"], "p_pf1": best["p_pf1"],
        }

    out = "/tmp/bos_quant_prob.json"
    with open(out, "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "ship_candidates": ship,
            "scorecard": {"p_win_min": 0.90, "p_pf1_min": 0.80, "n_min": 20, "q_pos_min": 3},
            "n_bootstrap": N_BOOT,
        }, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
