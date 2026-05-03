"""Kalman filter head-to-head: does swapping in Kalman on any symbol beat that
symbol's currently-deployed filter? Same harness as filter_swap_test.py
(2026-04-21) but adds Kalman as a 4th variant.

No auto-deploy. Print table → human decides.
"""
import os
import sys
import json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb


LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "FARTCOIN", "ENA", "SOL", "xyz:CL"]
# LIT omitted — just disabled in settings.py tonight per lit_verdict result.


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


def main():
    deployed = load_all()
    print(f"[1/2] Fetching data for {len(LIVE_SYMBOLS)} symbols...")
    arrs = {}
    for sym in LIVE_SYMBOLS:
        if sym not in deployed:
            print(f"   {sym}: no deployed config, skip"); continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        if len(d15) < 500 or len(d1h) < 100:
            print(f"   {sym}: insufficient data, skip"); continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

    print(f"\n[2/2] Head-to-head: current vs Kalman (params held fixed, only filter varies)\n")
    print(f"{'SYM':<10} {'CUR_FLT':<14} {'CUR n':>5} {'CUR $':>7} {'CUR PF':>6} "
          f"| {'KAL n':>5} {'KAL $':>7} {'KAL PF':>6} | {'delta $':>8} {'BEST':<8}")
    print("-" * 110)

    results = {}
    for sym, arr in arrs.items():
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        cfg_cur = _cfg_from_deployed(deployed[sym])
        cfg_kal = replace(cfg_cur, trend_filter_1h="kalman")

        s_cur = cb.stats(cb.backtest(arr, cfg_cur, lev))
        s_kal = cb.stats(cb.backtest(arr, cfg_kal, lev))

        delta = s_kal["pnl"] - s_cur["pnl"]
        best = "kalman" if s_kal["pnl"] > s_cur["pnl"] else "current"

        print(f"{sym:<10} {cfg_cur.trend_filter_1h:<14} {s_cur['n']:>5} "
              f"${s_cur['pnl']:>+6.0f} {s_cur['pf'] or 0:>6} "
              f"| {s_kal['n']:>5} ${s_kal['pnl']:>+6.0f} {s_kal['pf'] or 0:>6} "
              f"| ${delta:>+6.0f} {best:<8}")

        results[sym] = {"current_filter": cfg_cur.trend_filter_1h,
                        "current": s_cur, "kalman": s_kal,
                        "delta": delta, "best": best}

    kalman_wins = [s for s, r in results.items() if r["best"] == "kalman"]
    print(f"\nKalman wins on: {kalman_wins}")
    total_cur = sum(r["current"]["pnl"] for r in results.values())
    total_kal = sum(r["kalman"]["pnl"] for r in results.values())
    print(f"\nPortfolio totals over ~41 days:")
    print(f"  current filters: ${total_cur:+.0f}")
    print(f"  all-kalman:      ${total_kal:+.0f}")
    print(f"  delta:           ${total_kal - total_cur:+.0f}")

    with open("/tmp/kalman_swap_test.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull → /tmp/kalman_swap_test.json")


if __name__ == "__main__":
    main()
