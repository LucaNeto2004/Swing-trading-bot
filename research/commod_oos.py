"""Out-of-sample + walk-forward validation for whale_swing on HL commodity perps.

Procedure per symbol:
  1. Grid-search configs on IN-SAMPLE (first 70% of bars)
  2. Elect best by profit factor (min 10 IS trades)
  3. Score elected config on OUT-OF-SAMPLE (last 30%)
  4. Quartile split of FULL sample — edge must be positive or flat in all 4
  5. Random-entry benchmark (same exits, randomized long/short triggers)
  6. ±20% parameter sensitivity on sl_atr and tp multipliers

Pass criteria (per master validation scorecard):
  - OOS PF ≥ 1.2 AND OOS P&L > 0
  - All 4 quartiles non-negative P&L (or only one marginally negative)
  - Beats random by PF margin ≥ 0.3
  - Sensitivity: ±20% param perturbation keeps PF ≥ 1.0

Writes /tmp/commod_oos.json with full breakdown.
"""
import os
import sys
import json
import time
from dataclasses import dataclass, replace
from itertools import product

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd

from research.commod_backtest import (
    fetch_hl, add_features, precompute, backtest, stats, Cfg,
    SYMBOLS, LEV_CAP, COMMISSION,
)

IS_FRAC = 0.70  # 70% in-sample / 30% out-of-sample


def grid():
    entries = ["bb_touch", "ema_bounce", "rsi_bounce", "swing_pivot"]
    filter_1h = ["ema_cross", "both_agree", "hma_slope"]
    sls = [1.5, 2.0]
    trails = [0.0, 1.5]
    for et, f1h, sl, tr in product(entries, filter_1h, sls, trails):
        yield Cfg(
            trend_filter="ema_slope",
            entry_type=et,
            rsi_oversold=35.0, rsi_overbought=65.0,
            sl_atr=sl,
            tp1_atr=2.0, tp1_pct=0.3,
            tp2_atr=3.0, tp2_pct=0.3,
            tp3_atr=4.0, tp3_pct=0.2,
            trail_atr=tr,
            max_hold_bars=480,
            direction="both",
            use_1h_filter=True,
            trend_filter_1h=f1h,
            require_4h_agreement=False,
        )


def bt_range(arr, cfg, lev, i_start, i_end):
    """Backtest a sliced range [i_start, i_end) without modifying backtest()."""
    # The existing backtest() runs [max(i_start,52), len), so we need to
    # truncate arr to i_end. Build a shallow slice.
    sliced = {k: (v[:i_end] if hasattr(v, "__getitem__") else v) for k, v in arr.items()}
    return backtest(sliced, cfg, lev, i_start=max(i_start, 52))


def quartile_split(trades):
    if not trades:
        return []
    ts = sorted(trades, key=lambda t: t["ts"])
    q_bounds = [0, len(ts)//4, len(ts)//2, 3*len(ts)//4, len(ts)]
    out = []
    for q in range(4):
        chunk = ts[q_bounds[q]:q_bounds[q+1]]
        s = stats(chunk)
        out.append({"q": q + 1, **s})
    return out


def random_benchmark(arr, cfg, lev, seed=42, n_runs=5):
    """Match trade count + exits, but flip signals randomly. Average PF/P&L
    across n_runs to smooth variance."""
    rng = np.random.default_rng(seed)
    n = len(arr["close"])
    pfs, pnls, counts = [], [], []
    for _ in range(n_runs):
        # Build a fake array where rsi, ema, bb signals are randomized to
        # induce random entries at the same approximate rate
        rand_arr = dict(arr)
        # Flip up_1h / dn_1h arrays to random booleans with same density
        for k in ("up_1h", "dn_1h", "up_struct", "dn_struct", "up_4h", "dn_4h"):
            orig = arr[k].astype(bool)
            density = orig.mean()
            rand_arr[k] = rng.random(n) < density
        trades = backtest(rand_arr, cfg, lev)
        s = stats(trades)
        pfs.append(s["pf"] or 0.0)
        pnls.append(s["pnl"])
        counts.append(s["n"])
    return {
        "n_avg": float(np.mean(counts)),
        "pnl_avg": float(np.mean(pnls)),
        "pf_avg": float(np.mean(pfs)),
    }


def sensitivity(arr, cfg, lev):
    """±20% perturbation on sl_atr and tp*_atr. Edge must hold PF ≥ 1.0."""
    results = []
    for mult in (0.8, 1.2):
        pcfg = replace(
            cfg,
            sl_atr=cfg.sl_atr * mult,
            tp1_atr=cfg.tp1_atr * mult,
            tp2_atr=cfg.tp2_atr * mult,
            tp3_atr=cfg.tp3_atr * mult,
        )
        trades = backtest(arr, pcfg, lev)
        s = stats(trades)
        results.append({"mult": mult, **s})
    return results


def verdict(oos, quartiles, rnd, sens):
    reasons = []
    if oos["n"] < 10:
        reasons.append(f"OOS trades {oos['n']} < 10 (underpowered)")
    if (oos["pf"] or 0) < 1.2:
        reasons.append(f"OOS PF {oos['pf']} < 1.2")
    if oos["pnl"] <= 0:
        reasons.append(f"OOS P&L ${oos['pnl']} ≤ 0")
    negq = sum(1 for q in quartiles if q["pnl"] < 0)
    if negq > 1:
        reasons.append(f"{negq}/4 quartiles negative")
    if (oos["pf"] or 0) - (rnd["pf_avg"] or 0) < 0.3:
        reasons.append(f"Only {((oos['pf'] or 0) - (rnd['pf_avg'] or 0)):.2f} PF above random")
    for s in sens:
        if (s["pf"] or 0) < 1.0:
            reasons.append(f"±20% sens broke: mult={s['mult']} PF={s['pf']}")
            break
    return {"pass": not reasons, "fail_reasons": reasons}


def main():
    print(f"[1/4] Fetching HL 15m/1h/4h candles for {len(SYMBOLS)} symbols...")
    loaded = {}
    for sym in SYMBOLS:
        d15 = add_features(fetch_hl(sym, "15m", 4000))
        d1h = add_features(fetch_hl(sym, "1h", 2000))
        d4h = add_features(fetch_hl(sym, "4h", 1000))
        if len(d15) < 500 or len(d1h) < 100:
            print(f"   {sym}: insufficient data, skip")
            continue
        arr = precompute(d15, d1h, d4h)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<14} 15m={len(d15)} ({days}d)")
        loaded[sym] = (d15, arr)

    results = {}
    for sym, (d15, arr) in loaded.items():
        n = len(arr["close"])
        is_end = int(n * IS_FRAC)
        lev = LEV_CAP[sym] * 0.15
        print(f"\n{'='*72}\n{sym}  (n={n} bars, IS=[52..{is_end}) OOS=[{is_end}..{n}))\n{'='*72}")

        # [2/4] Grid search on IS
        print(f"  [2/4] IS grid ({sum(1 for _ in grid())} configs)...")
        is_runs = []
        for cfg in grid():
            trades_is = bt_range(arr, cfg, lev, i_start=52, i_end=is_end)
            s = stats(trades_is)
            is_runs.append((cfg, s))
        eligible = [(c, s) for c, s in is_runs if s["n"] >= 10 and s["pf"]]
        if not eligible:
            print("    No IS config met min trade count — skip")
            results[sym] = {"elected": None, "reason": "no eligible IS config"}
            continue
        best_cfg, best_is = max(eligible, key=lambda cs: (cs[1]["pf"], cs[1]["pnl"]))
        print(f"    IS BEST: {best_cfg.entry_type:<12} 1h={best_cfg.trend_filter_1h:<11} "
              f"sl={best_cfg.sl_atr} trail={best_cfg.trail_atr}  "
              f"n={best_is['n']} pnl=${best_is['pnl']:+.0f} pf={best_is['pf']} wr={best_is['wr']}%")

        # [3/4] Score elected on OOS only
        print(f"  [3/4] OOS verification...")
        # Trades that ENTER in OOS window. We run full backtest then filter
        # trades by entry timestamp ≥ arr['timestamp'][is_end]. Since the
        # backtest tracks one open position at a time, early-OOS positions
        # that carried over from IS are excluded — cleaner OOS.
        all_trades = backtest(arr, best_cfg, lev)
        oos_cutoff_ts = arr["timestamp"][is_end]
        oos_trades = [t for t in all_trades if t["ts"] >= oos_cutoff_ts]
        oos = stats(oos_trades)
        print(f"    OOS: n={oos['n']} pnl=${oos['pnl']:+.0f} pf={oos['pf']} wr={oos['wr']}% dd={oos['dd']}%")

        # [4/4] Quartiles / random / sensitivity on FULL sample
        quartiles = quartile_split(all_trades)
        print(f"  [4/4] Quartiles:")
        for q in quartiles:
            print(f"    Q{q['q']}: n={q['n']} pnl=${q['pnl']:+.0f} pf={q['pf']} wr={q['wr']}%")

        print(f"    Random-entry benchmark (5 runs)...")
        rnd = random_benchmark(arr, best_cfg, lev)
        print(f"    RND avg: n={rnd['n_avg']:.0f} pnl=${rnd['pnl_avg']:+.0f} pf={rnd['pf_avg']:.2f}")

        print(f"    ±20% parameter sensitivity...")
        sens = sensitivity(arr, best_cfg, lev)
        for s in sens:
            print(f"    mult={s['mult']}: n={s['n']} pnl=${s['pnl']:+.0f} pf={s['pf']} wr={s['wr']}%")

        v = verdict(oos, quartiles, rnd, sens)
        full = stats(all_trades)
        print(f"\n  VERDICT: {'PASS ✓' if v['pass'] else 'FAIL ✗'}")
        if v["fail_reasons"]:
            for r in v["fail_reasons"]:
                print(f"    - {r}")

        results[sym] = {
            "elected_cfg": {
                "entry_type": best_cfg.entry_type,
                "trend_filter_1h": best_cfg.trend_filter_1h,
                "sl_atr": best_cfg.sl_atr,
                "trail_atr": best_cfg.trail_atr,
                "tp1_atr": best_cfg.tp1_atr, "tp1_pct": best_cfg.tp1_pct,
                "tp2_atr": best_cfg.tp2_atr, "tp2_pct": best_cfg.tp2_pct,
                "tp3_atr": best_cfg.tp3_atr, "tp3_pct": best_cfg.tp3_pct,
                "rsi_oversold": best_cfg.rsi_oversold,
                "rsi_overbought": best_cfg.rsi_overbought,
                "max_hold_bars": best_cfg.max_hold_bars,
                "direction": best_cfg.direction,
            },
            "is": best_is, "oos": oos, "full": full,
            "quartiles": quartiles, "random": rnd, "sensitivity": sens,
            "verdict": v,
        }

    print(f"\n{'='*72}\nSUMMARY\n{'='*72}")
    print(f"{'SYM':<14} {'VERDICT':<8} {'IS PF':>6} {'OOS PF':>7} {'OOS $':>8} {'Q neg':>6}  CONFIG")
    for sym, r in results.items():
        if r.get("elected_cfg") is None:
            print(f"{sym:<14} SKIP     —      —      —        —      {r.get('reason','')}")
            continue
        v = "PASS" if r["verdict"]["pass"] else "FAIL"
        negq = sum(1 for q in r["quartiles"] if q["pnl"] < 0)
        cfg = r["elected_cfg"]
        cfg_str = f"{cfg['entry_type']}·{cfg['trend_filter_1h']}·sl{cfg['sl_atr']}·tr{cfg['trail_atr']}"
        print(f"{sym:<14} {v:<8} {r['is']['pf']:>6} {r['oos']['pf'] or 0:>7} "
              f"${r['oos']['pnl']:>+6.0f}   {negq}/4   {cfg_str}")

    out = "/tmp/commod_oos.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull breakdown → {out}")


if __name__ == "__main__":
    main()
