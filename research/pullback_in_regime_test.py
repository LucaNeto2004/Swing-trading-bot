"""Backtest pullback_in_regime across 13 crypto symbols with probability gate.

Strategy:
  - Regime classifier: Hurst + ADX + 5-filter ensemble → {trend_up, trend_down, range, chop}
  - Pivot detection: fractal + 2-of-3 quant validators (RSI, BB, volume) + ATR move
  - Entry: trend_up/range + pivot_L → LONG; trend_down/range + pivot_H → SHORT
  - Exit: opposite-side valid pivot OR regime flip against us OR 3% SL OR max_hold
  - CHOP regime = no trade (cannot short in green regime, cannot long in red)

Scorecard (strict, same as ensemble test):
  n ≥ 20, P(win) ≥ 0.85, P(PF>1) ≥ 0.75, ≥3/4 quartiles positive, $ > 0
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
    N_BOOT, P_WIN_MIN, P_PF1_MIN, N_MIN,
)
from research.current_vs_ensemble import (
    LIVE, SYM_LEV_EXP, _patch_weekday, _cfg_from_deployed, summarize,
)

SYMBOLS_13 = [s for s in (LIVE + ["ETH", "ARB", "LINK", "PENDLE", "TIA", "OP", "INJ"])
              if s != "xyz:CL"]


def run(sym, lev):
    d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
    d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
    d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
    if len(d15) < 500:
        return None
    arr = cb.precompute(d15, d1h, d4h)
    _patch_weekday(arr, sym)
    cfg = cb.Cfg(
        trend_filter="none", entry_type="pullback_in_regime",
        rsi_oversold=30.0, rsi_overbought=70.0,
        sl_atr=0.0, tp1_atr=0.0, tp1_pct=0.0,
        tp2_atr=0.0, tp2_pct=0.0, tp3_atr=0.0, tp3_pct=0.0,
        trail_atr=0.0, max_hold_bars=1000,
        direction="both", use_1h_filter=False,
        trend_filter_1h="ema_cross", require_4h_agreement=False,
        exit_type="pullback_exit",
    )
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    boot = bootstrap(trades)
    return {
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "dd": full["dd"],
        "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        **boot,
    }


def main():
    print(f"{'SYM':<8} {'n':>3} {'PF':>5} {'$':>7} {'OOS $':>7} {'dd':>5} "
          f"{'Q+':>3} {'P(win)':>7} {'P(PF>1)':>8} {'$CI_lo':>7} {'$CI_hi':>7}  grade")
    print("-" * 115)

    passes = []
    total_all = 0; total_pass = 0
    n_total_all = 0; n_total_pass = 0

    for sym in SYMBOLS_13:
        lev = (INSTRUMENTS.get(sym).hl_max_leverage if sym in INSTRUMENTS
               else SYM_LEV_EXP.get(sym, 10)) * 0.15
        try:
            r = run(sym, lev)
            if r is None or r["n"] == 0:
                print(f"{sym:<8} {0:>3}  (no trades / insufficient data)")
                continue
        except Exception as e:
            print(f"{sym:<8} ERROR: {e}")
            continue

        if r["n"] < N_MIN:                     g = "small"
        elif r["pnl"] <= 0:                    g = "unprof"
        elif r["p_win"] < P_WIN_MIN:           g = f"P(w)={r['p_win']:.2f}<{P_WIN_MIN}"
        elif r["p_pf1"] < P_PF1_MIN:           g = f"P(PF>1)<{P_PF1_MIN}"
        elif r["quarts_pos"] < 3:              g = "Q unstable"
        else:                                  g = "PASS"

        if g == "PASS":
            passes.append(sym); total_pass += r["pnl"]; n_total_pass += r["n"]
        total_all += r["pnl"]; n_total_all += r["n"]

        pf_s = f"{r['pf']:.2f}" if r['pf'] else "—"
        print(f"{sym:<8} {r['n']:>3} {pf_s:>5} ${r['pnl']:>+5.0f} ${r['oos_pnl']:>+5.0f} "
              f"{r['dd']:>5.1f} {r['quarts_pos']:>3} "
              f"{r['p_win']:>7.2f} {r['p_pf1']:>8.2f} "
              f"${r['pnl_lo']:>+5.0f} ${r['pnl_hi']:>+5.0f}  {g}")

    print()
    print(f"TOTAL all:   ${total_all:+.0f} on {n_total_all} trades, {len(SYMBOLS_13)} symbols")
    print(f"TOTAL pass:  ${total_pass:+.0f} on {n_total_pass} trades, {len(passes)} symbols")
    print(f"PASSING: {', '.join(passes) if passes else '(none)'}")


if __name__ == "__main__":
    main()
