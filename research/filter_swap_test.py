"""Targeted test: hold each symbol's currently DEPLOYED whale_swing config fixed,
only swap trend_filter_1h between variants, and compare head-to-head over full
available history. Answers: "does HMA specifically help vs the current filter?"

Output table: symbol | current filter | current PF/$ | hma PF/$ | delta.
"""
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dataclasses import replace
import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb

LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "FARTCOIN", "LIT", "ENA", "SOL", "xyz:CL"]


def _patch_weekday(arr, symbol):
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def _cfg_from_deployed(d: dict) -> cb.Cfg:
    return cb.Cfg(
        trend_filter=d["trend_filter"],
        entry_type=d["entry_type"],
        rsi_oversold=float(d["rsi_oversold"]),
        rsi_overbought=float(d["rsi_overbought"]),
        sl_atr=float(d["sl_atr"]),
        tp1_atr=float(d["tp1_atr"]),
        tp1_pct=float(d["tp1_pct"]),
        tp2_atr=float(d.get("tp2_atr", 0.0)),
        tp2_pct=float(d.get("tp2_pct", 0.0)),
        tp3_atr=float(d.get("tp3_atr", 0.0)),
        tp3_pct=float(d.get("tp3_pct", 0.0)),
        trail_atr=float(d["trail_atr"]),
        max_hold_bars=int(d["max_hold_bars"]),
        direction=d["direction"],
        use_1h_filter=bool(d["use_1h_filter"]),
        trend_filter_1h=d.get("trend_filter_1h", "ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement", False)),
    )


def main():
    deployed = load_all()
    print(f"[1/2] Fetching data for {len(LIVE_SYMBOLS)} symbols...")
    arrs = {}
    for sym in LIVE_SYMBOLS:
        if sym not in deployed:
            print(f"   {sym}: no deployed config, skip")
            continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        if len(d15) < 500 or len(d1h) < 100:
            print(f"   {sym}: insufficient data, skip")
            continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<14} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

    print(f"\n[2/2] Head-to-head: current vs HMA vs SJM (params held fixed, only filter varies)\n")
    print(f"{'SYM':<10} {'CUR_FLT':<12} {'CUR n':>5} {'CUR $':>7} {'CUR PF':>6} "
          f"| {'HMA n':>5} {'HMA $':>7} {'HMA PF':>6} "
          f"| {'SJM n':>5} {'SJM $':>7} {'SJM PF':>6} | {'BEST':<8}")
    print("-" * 130)

    results = {}
    for sym, arr in arrs.items():
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        cfg_cur = _cfg_from_deployed(deployed[sym])
        cfg_hma = replace(cfg_cur, trend_filter_1h="hma_slope")
        cfg_sjm = replace(cfg_cur, trend_filter_1h="sjm")

        s_cur = cb.stats(cb.backtest(arr, cfg_cur, lev))
        s_hma = cb.stats(cb.backtest(arr, cfg_hma, lev))
        s_sjm = cb.stats(cb.backtest(arr, cfg_sjm, lev))

        pnls = {"current": s_cur["pnl"], "hma": s_hma["pnl"], "sjm": s_sjm["pnl"]}
        best = max(pnls, key=pnls.get)

        print(f"{sym:<10} {cfg_cur.trend_filter_1h:<12} {s_cur['n']:>5} "
              f"${s_cur['pnl']:>+6.0f} {s_cur['pf'] or 0:>6} "
              f"| {s_hma['n']:>5} ${s_hma['pnl']:>+6.0f} {s_hma['pf'] or 0:>6} "
              f"| {s_sjm['n']:>5} ${s_sjm['pnl']:>+6.0f} {s_sjm['pf'] or 0:>6} "
              f"| {best:<8}")

        results[sym] = {
            "current_filter": cfg_cur.trend_filter_1h,
            "current": s_cur, "hma": s_hma, "sjm": s_sjm,
            "delta_hma": s_hma["pnl"] - s_cur["pnl"],
            "delta_sjm": s_sjm["pnl"] - s_cur["pnl"],
            "best": best,
        }

    print()
    best_counts = {"current": [], "hma": [], "sjm": []}
    for sym, r in results.items():
        best_counts[r["best"]].append(sym)
    print(f"Current filter wins: {best_counts['current']}")
    print(f"HMA wins:            {best_counts['hma']}")
    print(f"SJM wins:            {best_counts['sjm']}")

    # Aggregate portfolio P&L across the three filters
    totals = {k: sum(results[s][k]["pnl"] for s in results) for k in ("current", "hma", "sjm")}
    print(f"\nPortfolio totals (41 days, 10 symbols):")
    for k, v in totals.items():
        print(f"  {k:<8}: ${v:+.0f}")

    with open("/tmp/whale_filter_swap.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull → /tmp/whale_filter_swap.json")


if __name__ == "__main__":
    main()
