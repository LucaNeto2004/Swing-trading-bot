"""OOS validation for whale_swing across ALL live bot symbols (not just commodities).

Reuses commod_oos machinery but:
  - expands SYMBOLS to the 10 live bot symbols
  - pulls per-symbol leverage from INSTRUMENTS
  - neutralizes the weekday-only gate for 24/7 crypto symbols
  - grid now includes 'hma_slope' as a 1h filter option (new, no-lag filter)

Output: /tmp/whale_oos.json with full per-symbol breakdown + summary.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import numpy as np

from config.settings import INSTRUMENTS
import research.commod_backtest as cb
import research.commod_oos as oos

# Live whale bot universe (must match main.py runtime)
LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "FARTCOIN", "LIT", "ENA", "SOL", "xyz:CL"]


def _monkey_patch_universe():
    """Override commod_backtest globals so OOS pipeline targets live symbols."""
    cb.SYMBOLS = LIVE_SYMBOLS
    cb.LEV_CAP = {s: INSTRUMENTS[s].hl_max_leverage for s in LIVE_SYMBOLS}
    oos.SYMBOLS = LIVE_SYMBOLS
    oos.LEV_CAP = cb.LEV_CAP


def _patch_weekday_for_crypto(arr, symbol):
    """Crypto trades 24/7 — commod_backtest sets weekday mask to Mon–Fri which
    would blindly skip ~30% of crypto bars. xyz:* symbols keep the weekday
    gate (HIP-3 commodity perps close on weekends)."""
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def main():
    _monkey_patch_universe()

    # Re-bind the names the OOS main() uses via attribute lookup. Simpler:
    # just inline the loop so we can inject the weekday patch cleanly.
    print(f"[1/4] Fetching HL 15m/1h/4h candles for {len(LIVE_SYMBOLS)} live symbols...")
    loaded = {}
    for sym in LIVE_SYMBOLS:
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        if len(d15) < 500 or len(d1h) < 100:
            print(f"   {sym}: insufficient data, skip")
            continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday_for_crypto(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<14} 15m={len(d15)} ({days}d)")
        loaded[sym] = (d15, arr)

    results = {}
    for sym, (d15, arr) in loaded.items():
        n = len(arr["close"])
        is_end = int(n * oos.IS_FRAC)
        lev = cb.LEV_CAP[sym] * 0.15
        print(f"\n{'='*72}\n{sym}  (n={n} bars, IS=[52..{is_end}) OOS=[{is_end}..{n}))\n{'='*72}")

        print(f"  [2/4] IS grid ({sum(1 for _ in oos.grid())} configs)...")
        is_runs = []
        for cfg in oos.grid():
            trades_is = oos.bt_range(arr, cfg, lev, i_start=52, i_end=is_end)
            s = cb.stats(trades_is)
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
        print(f"  VERDICT: {'PASS ✓' if v['pass'] else 'FAIL ✗'}")
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
            "is": best_is, "oos": oos_stats, "full": full,
            "quartiles": quartiles, "random": rnd, "sensitivity": sens,
            "verdict": v,
        }

    print(f"\n{'='*72}\nSUMMARY — all 1h filter variants in grid (incl. hma_slope)\n{'='*72}")
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

    out = "/tmp/whale_oos.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull breakdown → {out}")


if __name__ == "__main__":
    main()
