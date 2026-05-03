"""A/B: would doing the OPPOSITE of every bot trade have been more profitable?

User's 2026-04-22 hunch. Same setups fire at the same bars (all filters,
RSI triggers, structure gates unchanged) — we just swap long↔short at the
moment of entry. SL/TP distances stay the same ATR multiples, just on the
opposite side of entry price.

This is a clean null-hypothesis check. If inverting is profitable, the
strategy's directional signal is actively wrong. If inverting LOSES MORE,
the strategy does have real directional edge — even when net P&L looks flat.

Portfolio P&L with each symbol's deployed config on 41d HL data.
"""
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb

LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "kPEPE", "FARTCOIN", "ENA", "SOL", "xyz:CL"]


def _patch_weekday(arr, symbol):
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def _cfg_from_deployed(d):
    return cb.Cfg(
        trend_filter=d["trend_filter"], entry_type=d["entry_type"],
        rsi_oversold=float(d["rsi_oversold"]), rsi_overbought=float(d["rsi_overbought"]),
        sl_atr=float(d["sl_atr"]),
        tp1_atr=float(d["tp1_atr"]), tp1_pct=float(d["tp1_pct"]),
        tp2_atr=float(d.get("tp2_atr", 0.0)), tp2_pct=float(d.get("tp2_pct", 0.0)),
        tp3_atr=float(d.get("tp3_atr", 0.0)), tp3_pct=float(d.get("tp3_pct", 0.0)),
        trail_atr=float(d["trail_atr"]), max_hold_bars=int(d["max_hold_bars"]),
        direction=d["direction"], use_1h_filter=bool(d["use_1h_filter"]),
        trend_filter_1h=d.get("trend_filter_1h", "ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement", False)),
    )


def _run(arrs, deployed):
    results = {}
    total = 0.0
    for sym, arr in arrs.items():
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        trades = cb.backtest(arr, _cfg_from_deployed(deployed[sym]), lev)
        s = cb.stats(trades)
        results[sym] = s
        total += s["pnl"]
    return results, total


def main():
    deployed = load_all()
    print(f"[1/3] Fetching {len(LIVE_SYMBOLS)} symbols...")
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
        arrs[sym] = arr

    print("[2/3] NORMAL direction (baseline)...")
    cb.INVERT_SIDE = False
    norm_rows, norm_total = _run(arrs, deployed)

    print("[3/3] INVERTED direction (long↔short)...")
    cb.INVERT_SIDE = True
    inv_rows, inv_total = _run(arrs, deployed)
    cb.INVERT_SIDE = False  # restore

    print(f"\n{'SYM':<10} {'NORM n':>6} {'NORM $':>8} {'NORM PF':>7} | {'INV n':>5} {'INV $':>8} {'INV PF':>6} | {'Δ$':>9}")
    print("-" * 100)
    for sym in arrs:
        o = norm_rows[sym]; n = inv_rows[sym]
        d = n["pnl"] - o["pnl"]
        print(f"{sym:<10} {o['n']:>6} ${o['pnl']:>+6.0f} {o['pf'] or 0:>7} | "
              f"{n['n']:>5} ${n['pnl']:>+6.0f} {n['pf'] or 0:>6} | ${d:>+7.0f}")

    print(f"\nPortfolio:   NORMAL=${norm_total:+.0f}   INVERTED=${inv_total:+.0f}   Δ=${inv_total-norm_total:+.0f}")
    if inv_total > norm_total:
        print("→ Inverting WOULD have been more profitable. Signal is contrarian.")
    else:
        print("→ Keeping normal direction. Strategy has real directional edge.")

    with open("/tmp/invert_ab.json", "w") as f:
        json.dump({"normal": norm_rows, "inverted": inv_rows,
                   "normal_total": norm_total, "inverted_total": inv_total}, f, indent=2, default=str)
    print("Full → /tmp/invert_ab.json")


if __name__ == "__main__":
    main()
