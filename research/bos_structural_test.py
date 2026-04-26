"""BOS-structural strategy OOS test.

Three variants per symbol:
  A. PURE BOS: entry on pivot break, exit on opposing pivot break. No SL, no TP.
  B. BOS HYBRID: entry on pivot break, TP1 partial at 2×ATR, rest rides to
                 opposing pivot break.
  C. CURRENT (baseline): whatever the deployed config runs today.

All held constant per-symbol except entry_type + exit_type (and TP1 for hybrid).
OOS-graded on the same scorecard: PF >= 1.1, OOS PF >= 1.0, >= 3/4 quartiles
positive, n >= 20, $ > 0.

NO auto-deploy. Report + JSON out.
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
        "label": label, "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "wr": full.get("wr"),
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "is_pf": split["is"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
        "cfg": {"entry": cfg.entry_type, "exit": cfg.exit_type,
                "filter": cfg.trend_filter_1h, "4h": cfg.require_4h_agreement,
                "direction": cfg.direction},
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

    print(f"\n[2/2] PURE BOS vs HYBRID BOS vs current deployed\n")

    all_results = {}
    for sym, arr in arrs.items():
        print(f"\n=== {sym} ===")
        dep = deployed[sym]
        base_cfg = _cfg_from_deployed(dep)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15

        current = run(arr, base_cfg, "current (deployed)", lev)
        current["grade"] = grade(current)

        # PURE BOS — entry bos_structural, exit bos_structural, no TP
        pure = run(arr, base_cfg, "PURE bos", lev,
                   entry_type="bos_structural", exit_type="bos_structural",
                   tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0)
        pure["grade"] = grade(pure)

        # HYBRID BOS — entry bos_structural, TP1 partial at 2×ATR, rest on BOS exit
        hybrid = run(arr, base_cfg, "HYBRID bos (TP1+BOS)", lev,
                     entry_type="bos_structural", exit_type="bos_hybrid",
                     tp1_atr=2.0, tp1_pct=0.3,
                     tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0)
        hybrid["grade"] = grade(hybrid)

        variants = [current, pure, hybrid]
        print(f"  {'VARIANT':<26} {'n':>4} {'PF':>6} {'$':>8} {'OOS PF':>8} {'Q+':>3}  grade")
        for v in variants:
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {v['label']:<26} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {oos:>8} "
                  f"{v['quarts_pos']:>3}  {v['grade']}")

        all_results[sym] = {"current": current, "pure": pure, "hybrid": hybrid}

    # Summary
    print(f"\n{'='*100}")
    print(f"  SUMMARY — BOS-structural vs deployed")
    print(f"{'='*100}")
    print(f"{'SYM':<10} {'CUR $':>8} {'PURE $':>8} {'PURE grade':<18} {'HYB $':>8} {'HYB grade':<18}")
    swap_queue = []
    for sym, r in all_results.items():
        c = r["current"]; p = r["pure"]; h = r["hybrid"]
        print(f"{sym:<10} ${c['pnl']:>+6.0f} ${p['pnl']:>+6.0f} {p['grade']:<18} "
              f"${h['pnl']:>+6.0f} {h['grade']:<18}")
        # Swap candidates
        for variant_name, v in (("pure", p), ("hybrid", h)):
            if v["grade"] == "PASS" and v["pnl"] - c["pnl"] > 50:
                swap_queue.append((sym, variant_name, c["pnl"], v["pnl"]))

    print(f"\nSwap candidates (PASS + Δ>$50):")
    for sym, name, cur_pnl, new_pnl in swap_queue:
        print(f"  {sym}: {name}  ${cur_pnl:+.0f} → ${new_pnl:+.0f}  (Δ ${new_pnl-cur_pnl:+.0f})")
    if not swap_queue:
        print("  none")

    with open("/tmp/bos_structural_test.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull → /tmp/bos_structural_test.json")


if __name__ == "__main__":
    main()
