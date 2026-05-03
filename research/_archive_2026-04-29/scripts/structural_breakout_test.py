"""Structural breakout entry-type OOS test across all active symbols.

For each symbol, holds EVERYTHING ELSE from its deployed config fixed and
swaps ONLY the entry_type to 'structural_breakout'. Compares:
  - current (deployed entry_type) vs structural_breakout
  - also tries structural_breakout with each of the 6 filter variants (in case
    the breakout pairs better with a different filter than the current one)

Scorecard gate: OOS PF >= 1.0, n >= 20, >= 3/4 quartiles positive, $ > 0.

NO AUTO-DEPLOY. Prints table + writes JSON. Human decides.
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


def run(arr, base_cfg, label, lev, **overrides):
    cfg = replace(base_cfg, **overrides)
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    return {
        "label": label, "n": full["n"], "pf": full["pf"], "pnl": full["pnl"],
        "wr": full.get("wr"),
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"],
        "oos_pf": split["oos"]["pf"],
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

    print(f"\n[2/2] structural_breakout vs deployed, per symbol × 6 filters\n")

    all_results = {}
    for sym, arr in arrs.items():
        print(f"\n=== {sym} ===")
        dep = deployed[sym]
        base_cfg = _cfg_from_deployed(dep)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15

        current = run(arr, base_cfg, f"deployed ({dep['entry_type']}/{dep.get('trend_filter_1h')})", lev)
        current["grade"] = grade(current)
        print(f"  Deployed: n={current['n']} PF={current['pf']} "
              f"${current['pnl']:+.0f} Q+={current['quarts_pos']} → {current['grade']}")

        variants = []
        for fv in FILTER_VARIANTS:
            v = run(arr, base_cfg, f"sb/{fv}", lev,
                    entry_type="structural_breakout", trend_filter_1h=fv)
            v["grade"] = grade(v)
            variants.append(v)

        # Sort by $
        variants_sorted = sorted(variants, key=lambda v: v["pnl"], reverse=True)
        print(f"  {'FILTER':<14} {'n':>4} {'PF':>6} {'$':>8} {'OOS_PF':>8} {'Q+':>3}  grade")
        for v in variants_sorted:
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {v['label'][3:]:<14} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {oos:>8} "
                  f"{v['quarts_pos']:>3}  {v['grade']}")

        best_sb = max(variants, key=lambda v: v["pnl"])
        all_results[sym] = {
            "deployed": current,
            "best_sb": best_sb,
            "all_sb": variants,
        }

    # Summary
    print(f"\n{'='*100}")
    print(f"  SUMMARY — structural_breakout candidate per symbol")
    print(f"{'='*100}")
    print(f"{'SYM':<10} {'Deployed $':>11} {'Best SB':<20} {'SB $':>8} {'SB grade':<16} {'Δ vs dep':>10}")
    swap_queue = []
    for sym, r in all_results.items():
        dep = r["deployed"]; sb = r["best_sb"]
        delta = sb["pnl"] - dep["pnl"]
        label = sb["label"][3:]  # strip "sb/"
        print(f"{sym:<10} ${dep['pnl']:>+8.0f}   "
              f"{label:<20} ${sb['pnl']:>+6.0f} {sb['grade']:<16} ${delta:>+8.0f}")
        if sb["grade"] == "PASS" and delta > 50:
            swap_queue.append((sym, dep, sb))

    print(f"\nSwap candidates (PASS + Δ>$50): {[s[0] for s in swap_queue]}")
    with open("/tmp/structural_breakout_test.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"Full → /tmp/structural_breakout_test.json")


if __name__ == "__main__":
    main()
