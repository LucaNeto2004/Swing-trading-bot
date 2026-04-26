"""BOS entry × quant confirmation layers.

Per-symbol, holds deployed params fixed and tests 6 variants:
  V0 current deployed (baseline)
  V1 PURE bos (from last test)
  V2 HYBRID bos
  V3 PURE bos + funding-extreme confirmation
  V4 PURE bos + 4h_agreement
  V5 HYBRID bos + funding-extreme confirmation

Each graded via scorecard. Goal: does a quant confirmation layer make
BOS more robust (better OOS PF, more stable quartiles) even if total $ drops?
"""
from __future__ import annotations

import os
import sys
import json
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared")))

import numpy as np
import pandas as pd

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb
from core.features import add_funding_features

import hl_client  # noqa: E402


LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "ENA", "SOL", "xyz:CL"]


def _patch_weekday(arr, symbol):
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def _augment_with_funding(arr, symbol, df15):
    """Add funding_extreme array to arr, aligned to 15m bars."""
    if symbol.startswith("xyz:"):
        arr["funding_extreme"] = np.zeros(len(df15), dtype=int)
        return
    try:
        t0_ms = int(df15["timestamp"].iloc[0].timestamp() * 1000)
        t1_ms = int(df15["timestamp"].iloc[-1].timestamp() * 1000)
        funding = hl_client.sync_get_funding_history(symbol, t0_ms - 86400*1000, t1_ms)
        if funding.empty:
            arr["funding_extreme"] = np.zeros(len(df15), dtype=int)
            return
        enriched = add_funding_features(df15, funding)
        arr["funding_extreme"] = enriched["funding_extreme"].to_numpy()
    except Exception as e:
        print(f"   funding augment failed for {symbol}: {e}")
        arr["funding_extreme"] = np.zeros(len(df15), dtype=int)


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
    print(f"[1/2] Fetching data + funding for {len(LIVE_SYMBOLS)} symbols...")
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
        _augment_with_funding(arr, sym, d15)
        n_ext_pos = int((arr["funding_extreme"] == 1).sum())
        n_ext_neg = int((arr["funding_extreme"] == -1).sum())
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)  funding_ext: +={n_ext_pos} -={n_ext_neg}")
        arrs[sym] = arr

    print(f"\n[2/2] Testing 6 variants per symbol\n")

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
        v3 = run(arr, base_cfg, "V3 PURE+fund", lev,
                 entry_type="bos_structural", exit_type="bos_structural",
                 tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                 require_funding_confirm=True)
        v4 = run(arr, base_cfg, "V4 PURE+4h", lev,
                 entry_type="bos_structural", exit_type="bos_structural",
                 tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                 require_4h_agreement=True)
        v5 = run(arr, base_cfg, "V5 HYBRID+fund", lev,
                 entry_type="bos_structural", exit_type="bos_hybrid",
                 tp1_atr=2.0, tp1_pct=0.3,
                 tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                 require_funding_confirm=True)

        variants = [v0, v1, v2, v3, v4, v5]
        for v in variants: v["grade"] = grade(v)
        print(f"  {'VARIANT':<18} {'n':>4} {'PF':>6} {'$':>8} {'OOS PF':>8} {'Q+':>3}  grade")
        for v in variants:
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {v['label']:<18} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {oos:>8} "
                  f"{v['quarts_pos']:>3}  {v['grade']}")
        all_results[sym] = variants

    # Summary — best passing per symbol
    print(f"\n{'='*110}")
    print(f"  BEST PASSING VARIANT PER SYMBOL")
    print(f"{'='*110}")
    print(f"{'SYM':<10} {'CUR $':>8} {'BEST PASS':<20} {'BEST $':>8} {'OOS PF':>8} {'Q+':>3} {'Δ vs CUR':>10}")
    for sym, vs in all_results.items():
        cur = vs[0]
        passing = [v for v in vs if v["grade"] == "PASS"]
        if passing:
            best = max(passing, key=lambda v: v["pnl"])
            delta = best["pnl"] - cur["pnl"]
            oos = f"{best['oos_pf']:.2f}" if best['oos_pf'] else "—"
            print(f"{sym:<10} ${cur['pnl']:>+6.0f} {best['label']:<20} "
                  f"${best['pnl']:>+6.0f} {oos:>8} {best['quarts_pos']:>3} ${delta:>+8.0f}")
        else:
            print(f"{sym:<10} ${cur['pnl']:>+6.0f} (no passing alt)")

    with open("/tmp/bos_with_quant_test.json", "w") as f:
        json.dump({s: [dict(v) for v in vs] for s, vs in all_results.items()},
                  f, indent=2, default=str)
    print(f"\nFull → /tmp/bos_with_quant_test.json")


if __name__ == "__main__":
    main()
