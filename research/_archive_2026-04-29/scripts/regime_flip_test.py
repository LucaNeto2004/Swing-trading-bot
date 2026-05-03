"""3-way strategy comparison: current deployed vs pure BOS vs regime_flip.

Per-symbol, hold everything else constant, test:
  V0 current (deployed entry_type + exit)
  V1 PURE bos (entry=bos_structural, exit=bos_structural)
  V2 HYBRID bos (entry=bos_structural, exit=bos_hybrid, TP1 2x ATR partial)
  V3 PURE regime_flip (entry=regime_flip, exit=regime_flip)
  V4 HYBRID regime_flip (entry=regime_flip, exit=regime_flip_hybrid, TP1 2x ATR)

Scorecard gate same as before: OOS PF >= 1.0, n >= 20, >= 3/4 quartiles positive, $ > 0.

NO auto-deploy.
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


def _patch_weekday(arr, symbol):
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def _cfg_from_deployed(d):
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


def run(arr, base_cfg, label, lev, **overrides):
    cfg = replace(base_cfg, **overrides)
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    return {
        "label": label, "n": full["n"], "pf": full["pf"], "pnl": full["pnl"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
    }


def grade(v):
    if v["n"] < 20: return "small"
    if v["pnl"] <= 0: return "unprofitable"
    if v["oos_pf"] is None or v["oos_pf"] < 1.0: return "fails OOS"
    if v["quarts_pos"] < 3: return "quartiles unstable"
    return "PASS"


def main():
    deployed = load_all()
    print(f"[1/2] Fetching data...")
    arrs = {}
    for sym in LIVE_SYMBOLS:
        if sym not in deployed:
            continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        if len(d15) < 500:
            continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

    print(f"\n[2/2] V0 current vs V1 PURE bos vs V3 PURE regime_flip vs V4 HYBRID regime_flip\n")

    all_results = {}
    for sym, arr in arrs.items():
        print(f"\n=== {sym} ===")
        dep = deployed[sym]
        base_cfg = _cfg_from_deployed(dep)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15

        v0 = run(arr, base_cfg, "V0 current", lev)
        v1 = run(arr, base_cfg, "V1 PURE bos", lev,
                 entry_type="bos_structural", exit_type="bos_structural",
                 tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0)
        v2 = run(arr, base_cfg, "V2 HYBRID bos", lev,
                 entry_type="bos_structural", exit_type="bos_hybrid",
                 tp1_atr=2.0, tp1_pct=0.3,
                 tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0)
        v3 = run(arr, base_cfg, "V3 PURE regime", lev,
                 entry_type="regime_flip", exit_type="regime_flip",
                 tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0)
        v4 = run(arr, base_cfg, "V4 HYBRID regime", lev,
                 entry_type="regime_flip", exit_type="regime_flip_hybrid",
                 tp1_atr=2.0, tp1_pct=0.3,
                 tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0)

        variants = [v0, v1, v2, v3, v4]
        for v in variants: v["grade"] = grade(v)
        print(f"  {'VARIANT':<22} {'n':>4} {'PF':>6} {'$':>8} {'OOS PF':>8} {'Q+':>3}  grade")
        for v in variants:
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {v['label']:<22} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {oos:>8} "
                  f"{v['quarts_pos']:>3}  {v['grade']}")
        all_results[sym] = variants

    # Per-symbol best passing
    print(f"\n{'='*110}")
    print(f"  BEST PASSING VARIANT PER SYMBOL (3-way: current / BOS / regime_flip)")
    print(f"{'='*110}")
    print(f"{'SYM':<10} {'CUR $':>7} {'BOS best':<18} {'BOS $':>7} {'RGM best':<18} {'RGM $':>7} {'WINNER':<18}")
    for sym, vs in all_results.items():
        cur = vs[0]
        bos_variants = [v for v in (vs[1], vs[2]) if v["grade"] == "PASS"]
        rgm_variants = [v for v in (vs[3], vs[4]) if v["grade"] == "PASS"]
        bos_best = max(bos_variants, key=lambda v: v["pnl"]) if bos_variants else None
        rgm_best = max(rgm_variants, key=lambda v: v["pnl"]) if rgm_variants else None

        bos_str = f"{bos_best['label'][3:]} ${bos_best['pnl']:+.0f}" if bos_best else "(none pass)"
        rgm_str = f"{rgm_best['label'][3:]} ${rgm_best['pnl']:+.0f}" if rgm_best else "(none pass)"

        best_overall = max([c for c in (cur, bos_best, rgm_best) if c is not None],
                           key=lambda v: v["pnl"])
        winner = f"{best_overall['label']}"

        bos_label = bos_best['label'][3:] if bos_best else "—"
        bos_pnl = bos_best['pnl'] if bos_best else 0
        rgm_label = rgm_best['label'][3:] if rgm_best else "—"
        rgm_pnl = rgm_best['pnl'] if rgm_best else 0
        print(f"{sym:<10} ${cur['pnl']:>+6.0f} {bos_label:<18} ${bos_pnl:>+6.0f} "
              f"{rgm_label:<18} ${rgm_pnl:>+6.0f} {winner:<18}")

    with open("/tmp/regime_flip_test.json", "w") as f:
        json.dump({s: [dict(v) for v in vs] for s, vs in all_results.items()},
                  f, indent=2, default=str)
    print(f"\nFull → /tmp/regime_flip_test.json")


if __name__ == "__main__":
    main()
