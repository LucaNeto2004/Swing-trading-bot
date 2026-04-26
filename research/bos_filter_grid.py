"""Focused 4×2 filter grid on the 5 BOS/regime-deployed symbols.

For each of BTC, HYPE, SOL, XRP, kPEPE:
  - keep the deployed entry_type + exit_type
  - vary trend_filter_1h ∈ {sjm, hma_slope, kalman, both_agree}
  - vary require_4h_agreement ∈ {False, True}
  = 8 cells per symbol, 40 backtests total

Ship rule: only adopt a new cell if it
  (a) PASSES the scorecard (PF≥1, n≥20, ≥3/4 quartiles positive, $>0)
  (b) beats the current deployed baseline in OOS P&L

Writes /tmp/bos_filter_grid.json.
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


SYMBOLS = ["BTC", "HYPE", "SOL", "XRP", "kPEPE"]
FILTERS = ["sjm", "hma_slope", "kalman", "both_agree"]
FOUR_H  = [False, True]


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


def q_pnls(trades, n_q=4):
    if not trades:
        return [0.0] * n_q
    k = len(trades) // n_q
    if k == 0:
        return [sum(t["pnl"] for t in trades)] + [0.0] * (n_q - 1)
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


def run(arr, base, label, lev, **ov):
    cfg = replace(base, **ov)
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    return {
        "label": label,
        "filter_1h": cfg.trend_filter_1h,
        "req_4h": cfg.require_4h_agreement,
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "dd": full["dd"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "is_n":  split["is"]["n"],  "is_pnl":  split["is"]["pnl"],  "is_pf":  split["is"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
    }


def grade(v):
    """PF≥1, n≥20, ≥3/4 quartiles positive, $>0."""
    if v["n"] < 20:          return "small"
    if v["pnl"] <= 0:        return "unprofitable"
    if (v["pf"] or 0) < 1.0: return "PF<1"
    if v["quarts_pos"] < 3:  return "quartiles unstable"
    return "PASS"


def main():
    dep_all = load_all()
    print(f"[1/2] Fetching 15m/1h/4h for {len(SYMBOLS)} symbols...")
    arrs = {}
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
        arrs[sym] = arr

    print(f"\n[2/2] Grid: {len(FILTERS)} filters × {len(FOUR_H)} 4h-opts = {len(FILTERS)*len(FOUR_H)} per sym\n")

    all_results = {}
    for sym, arr in arrs.items():
        dep = dep_all[sym]
        base = _cfg_from_deployed(dep)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15

        print(f"\n=== {sym}  entry={base.entry_type}  exit={base.exit_type} ===")
        print(f"  {'FILTER':<12} {'4h':<5} {'n':>4} {'PF':>6} {'$':>8} {'dd%':>6} "
              f"{'OOS $':>8} {'OOSpf':>6} {'Q+':>3}  grade")

        variants = []
        # Variant 0 — current deployed (baseline)
        v0 = run(arr, base, "V0 CURRENT", lev)
        v0["is_baseline"] = True
        variants.append(v0)
        pf = f"{v0['pf']:.2f}" if v0['pf'] else "—"
        oos = f"{v0['oos_pf']:.2f}" if v0['oos_pf'] else "—"
        print(f"  {'[cur]'+v0['filter_1h']:<12} {str(v0['req_4h']):<5} {v0['n']:>4} "
              f"{pf:>6} ${v0['pnl']:>+6.0f} {v0['dd']:>6.1f} "
              f"${v0['oos_pnl']:>+6.0f} {oos:>6} {v0['quarts_pos']:>3}  baseline")

        for f1h, r4h in product(FILTERS, FOUR_H):
            if f1h == base.trend_filter_1h and r4h == base.require_4h_agreement:
                continue  # already have it as V0
            label = f"{f1h}/{'4hON' if r4h else '4hOFF'}"
            v = run(arr, base, label, lev,
                    trend_filter_1h=f1h, require_4h_agreement=r4h)
            v["grade"] = grade(v)
            variants.append(v)
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {f1h:<12} {str(r4h):<5} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} "
                  f"{v['dd']:>6.1f} ${v['oos_pnl']:>+6.0f} {oos:>6} {v['quarts_pos']:>3}  {v['grade']}")
        v0["grade"] = grade(v0)
        all_results[sym] = variants

    # Ship recommendations
    print(f"\n{'='*115}")
    print(f"  SHIP CANDIDATES — PASSING VARIANTS THAT BEAT CURRENT OOS $")
    print(f"{'='*115}")
    print(f"{'SYM':<8} {'CUR':<22} {'CUR $':>7} {'WINNER':<22} {'NEW $':>7} {'OOS $':>7} "
          f"{'OOSpf':>6} {'Q+':>3} {'Δ $':>8}")
    ship = {}
    for sym, vs in all_results.items():
        cur = vs[0]
        passing = [v for v in vs[1:] if v["grade"] == "PASS"]
        better = [v for v in passing if v["oos_pnl"] > cur["oos_pnl"]]
        if not better:
            print(f"{sym:<8} {cur['filter_1h']+'/'+('4hON' if cur['req_4h'] else '4hOFF'):<22} "
                  f"${cur['oos_pnl']:>+5.0f}  (no passing alt beats current OOS)")
            continue
        best = max(better, key=lambda v: v["oos_pnl"])
        delta = best["oos_pnl"] - cur["oos_pnl"]
        cur_label = cur['filter_1h']+('/4hON' if cur['req_4h'] else '/4hOFF')
        oos = f"{best['oos_pf']:.2f}" if best['oos_pf'] else "—"
        print(f"{sym:<8} {cur_label:<22} ${cur['oos_pnl']:>+5.0f} {best['label']:<22} "
              f"${best['pnl']:>+5.0f} ${best['oos_pnl']:>+5.0f} {oos:>6} {best['quarts_pos']:>3} "
              f"${delta:>+6.0f}")
        ship[sym] = {
            "filter_1h": best["filter_1h"],
            "require_4h_agreement": best["req_4h"],
            "oos_delta": delta,
        }

    out = "/tmp/bos_filter_grid.json"
    with open(out, "w") as f:
        json.dump({
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "ship_candidates": ship,
        }, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
