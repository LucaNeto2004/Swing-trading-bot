"""A/B test: run every live symbol's DEPLOYED config with TIME_STOP on/off.

Motivated by 0x1aa780bb… (HL trader, 2026-04-21 study): his median hold is
7h, avg loss only $2,719 vs avg win $18k — he cuts stale trades before they
bleed. Our bot's only time-gate is max_hold (15d ceiling). This quantifies
what time-stop buys (or costs) on held-params live config.

Output: portfolio P&L with vs without time-stop, per-symbol breakdown.
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


def _run(arrs, deployed):
    results = {}
    total = 0.0
    for sym, arr in arrs.items():
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        cfg = _cfg_from_deployed(deployed[sym])
        trades = cb.backtest(arr, cfg, lev)
        s = cb.stats(trades)
        # count time_stop exits if any
        n_ts = sum(1 for t in trades if t.get("reason") == "time_stop")
        results[sym] = {**s, "n_time_stop": n_ts}
        total += s["pnl"]
    return results, total


def main():
    deployed = load_all()
    print(f"[1/3] Fetching data for {len(LIVE_SYMBOLS)} symbols...")
    arrs = {}
    for sym in LIVE_SYMBOLS:
        if sym not in deployed:
            continue
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        if len(d15) < 500 or len(d1h) < 100:
            continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<14} 15m={len(d15)} ({days}d)")
        arrs[sym] = arr

    print("\n[2/3] TIME_STOP OFF (baseline)...")
    cb.TIME_STOP_ENABLED = False
    off_rows, off_total = _run(arrs, deployed)

    print("[3/3] TIME_STOP ON...")
    cb.TIME_STOP_ENABLED = True
    on_rows, on_total = _run(arrs, deployed)

    print(f"\n{'SYM':<10} {'OFF n':>5} {'OFF $':>8} {'OFF PF':>6} | {'ON n':>5} {'ON $':>8} {'ON PF':>6} {'TS exits':>9} | {'Δ$':>9}")
    print("-" * 100)
    for sym in arrs:
        o = off_rows[sym]; n = on_rows[sym]
        d = n["pnl"] - o["pnl"]
        print(f"{sym:<10} {o['n']:>5} ${o['pnl']:>+6.0f} {o['pf'] or 0:>6} | "
              f"{n['n']:>5} ${n['pnl']:>+6.0f} {n['pf'] or 0:>6} {n['n_time_stop']:>9} | ${d:>+7.0f}")

    print(f"\nPortfolio totals:  OFF=${off_total:+.0f}   ON=${on_total:+.0f}   Δ=${on_total-off_total:+.0f}")

    with open("/tmp/time_stop_ab.json", "w") as f:
        json.dump({"off": off_rows, "on": on_rows, "off_total": off_total, "on_total": on_total}, f, indent=2, default=str)
    print("Full → /tmp/time_stop_ab.json")


if __name__ == "__main__":
    main()
