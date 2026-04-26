"""TAO-only OOS validation run. Motivated by 0x1aa780bb… on HL — he made
+$476k on 12 TAO trades (50% WR) in 45 days, 89% of his total P&L from the
single coin. Gate TAO through the same whale_oos pipeline before deploying.

Not committing TAO to config/settings.py INSTRUMENTS until this passes.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import numpy as np

from config.settings import Instrument
import research.commod_backtest as cb
import research.commod_oos as oos


SYMBOL = "TAO"
LEV_MAX = 5  # HL-verified 2026-04-21: TAO maxLeverage=5


def main():
    # Pin universe for this run. We don't mutate INSTRUMENTS — just set the
    # backtester's SYMBOLS/LEV_CAP tables used by the OOS machinery.
    cb.SYMBOLS = [SYMBOL]
    cb.LEV_CAP = {SYMBOL: LEV_MAX}
    oos.SYMBOLS = [SYMBOL]
    oos.LEV_CAP = cb.LEV_CAP

    print(f"[1/4] Fetching HL 15m/1h/4h candles for {SYMBOL}...")
    d15 = cb.add_features(cb.fetch_hl(SYMBOL, "15m", 4000))
    d1h = cb.add_features(cb.fetch_hl(SYMBOL, "1h", 2000))
    d4h = cb.add_features(cb.fetch_hl(SYMBOL, "4h", 1000))
    if len(d15) < 500 or len(d1h) < 100:
        print(f"  insufficient data (15m={len(d15)} 1h={len(d1h)}) — abort")
        return
    arr = cb.precompute(d15, d1h, d4h)
    # Crypto: neutralize weekday mask
    arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
    days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
    print(f"  {SYMBOL}: 15m={len(d15)} ({days}d)")

    n = len(arr["close"])
    is_end = int(n * oos.IS_FRAC)
    lev = LEV_MAX * 0.15
    print(f"\n{SYMBOL}  (n={n} bars, IS=[52..{is_end}) OOS=[{is_end}..{n}))")

    n_cfgs = sum(1 for _ in oos.grid())
    print(f"  [2/4] IS grid ({n_cfgs} configs)...")
    is_runs = []
    for cfg in oos.grid():
        trades_is = oos.bt_range(arr, cfg, lev, i_start=52, i_end=is_end)
        s = cb.stats(trades_is)
        is_runs.append((cfg, s))
    eligible = [(c, s) for c, s in is_runs if s["n"] >= 10 and s["pf"]]
    if not eligible:
        print("    No IS config met min trade count — skip")
        return
    best_cfg, best_is = max(eligible, key=lambda cs: (cs[1]["pf"], cs[1]["pnl"]))
    print(f"    IS BEST: {best_cfg.entry_type:<12} 1h={best_cfg.trend_filter_1h:<11} "
          f"sl={best_cfg.sl_atr} trail={best_cfg.trail_atr} dir={best_cfg.direction} "
          f"n={best_is['n']} pnl=${best_is['pnl']:+.0f} pf={best_is['pf']} wr={best_is['wr']}%")

    all_trades = cb.backtest(arr, best_cfg, lev)
    oos_cutoff_ts = arr["timestamp"][is_end]
    oos_trades = [t for t in all_trades if t["ts"] >= oos_cutoff_ts]
    oos_stats = cb.stats(oos_trades)
    print(f"  [3/4] OOS: n={oos_stats['n']} pnl=${oos_stats['pnl']:+.0f} pf={oos_stats['pf']} wr={oos_stats['wr']}% dd={oos_stats['dd']}%")

    quartiles = oos.quartile_split(all_trades)
    print(f"  [4/4] Quartiles:")
    for q in quartiles:
        print(f"    Q{q['q']}: n={q['n']} pnl=${q['pnl']:+.0f} pf={q['pf']} wr={q['wr']}%")
    rnd = oos.random_benchmark(arr, best_cfg, lev)
    print(f"    RND avg: n={rnd['n_avg']:.0f} pnl=${rnd['pnl_avg']:+.0f} pf={rnd['pf_avg']:.2f}")
    sens = oos.sensitivity(arr, best_cfg, lev)
    for s in sens:
        print(f"    sens mult={s['mult']}: n={s['n']} pnl=${s['pnl']:+.0f} pf={s['pf']} wr={s['wr']}%")

    v = oos.verdict(oos_stats, quartiles, rnd, sens)
    full = cb.stats(all_trades)
    verdict_str = "PASS ✓" if v["pass"] else "FAIL ✗"
    print(f"\n  VERDICT: {verdict_str}")
    for r in v["fail_reasons"]:
        print(f"    - {r}")

    out = {
        "symbol": SYMBOL,
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
        "is": best_is, "oos": oos_stats, "full": full,
        "quartiles": quartiles, "random": rnd, "sensitivity": sens,
        "verdict": v,
    }
    with open("/tmp/tao_oos.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nFull → /tmp/tao_oos.json")


if __name__ == "__main__":
    main()
