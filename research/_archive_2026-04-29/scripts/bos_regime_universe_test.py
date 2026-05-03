"""BOS + quant regime confluence — universe screen.

Premise: widen the scan universe beyond the current 8 live symbols and only
trade high-conviction setups where a Break of Structure (1h pivot break) aligns
with a quant-regime filter (sjm / hma_slope / kalman). Exit is pure structural
(opposing-pivot close) — no SL/TP/trail. If the thesis is right, we should see
>8 symbols clearing the scorecard.

Scorecard per variant (per symbol):
  - n >= 15 trades
  - PF >= 1.1 full-sample
  - OOS PF >= 1.0 (70/30 IS/OOS split)
  - 3 of 4 quartiles positive
  - positive net P&L

Variants per symbol (6 total):
  V1 bos_structural + sjm
  V2 bos_structural + hma_slope
  V3 bos_structural + kalman
  V4 structural_breakout + sjm
  V5 structural_breakout + hma_slope
  V6 structural_breakout + kalman

Writes full results to /tmp/bos_regime_universe_test.json.
"""
from __future__ import annotations

import os
import sys
import json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

import research.commod_backtest as cb


UNIVERSE = [
    # majors
    "BTC", "ETH", "SOL", "HYPE", "XRP", "DOGE",
    # L1/L2 alts
    "AVAX", "LINK", "SUI", "NEAR", "APT", "ATOM", "TIA", "SEI", "INJ", "ADA",
    # DeFi + other
    "AAVE", "PENDLE", "LDO", "ONDO", "LTC",
    # meme / high-beta
    "kPEPE", "FARTCOIN", "WLD", "TAO",
    # rollup
    "OP", "ARB",
    # current + commodity
    "ENA", "ZEC", "xyz:CL",
]

# Leverage cap used in backtests — mirrors commod_backtest.py convention
# (margin_pct × lev_cap = effective exposure). xyz HIP-3 caps at 5×,
# everything else defaults to the 58bro-style 6× effective (15% × 40).
def _lev_for(sym: str) -> float:
    if sym.startswith("xyz:"):
        return 5.0 * 0.15  # 0.75x
    return 40.0 * 0.15     # 6.0x — same as whale_swing live sizing


REGIME_FILTERS = ["sjm", "hma_slope", "kalman"]
ENTRIES = ["bos_structural", "structural_breakout"]


def _patch_weekday(arr, symbol):
    if not symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)


def _base_cfg() -> cb.Cfg:
    # BOS-style config — no TP ladder, disaster_sl_atr=3.0 as the gap floor.
    # Structural exit (opposing-pivot close) still primary; disaster only fires
    # on wide-gap events where structural exit would have missed.
    return cb.Cfg(
        trend_filter="ema_slope",      # lightweight 5m filter
        entry_type="bos_structural",   # overridden per variant
        rsi_oversold=35.0, rsi_overbought=65.0,
        sl_atr=2.0,                    # unused when exit_type=bos_structural
        tp1_atr=0.0, tp1_pct=0.0,
        tp2_atr=0.0, tp2_pct=0.0,
        tp3_atr=0.0, tp3_pct=0.0,
        trail_atr=0.0,
        max_hold_bars=480,             # 5 days at 15m
        direction="both",
        use_1h_filter=True,            # KEY — regime filter must agree
        trend_filter_1h="sjm",         # overridden per variant
        require_4h_agreement=False,
        exit_type="bos_structural",    # pure structural exit
        disaster_sl_atr=3.0,           # gap protection floor
    )


def q_pnls(trades, n_q=4):
    if not trades:
        return [0.0] * n_q
    k = len(trades) // n_q
    if k == 0:
        return [sum(t["pnl"] for t in trades)] + [0.0] * (n_q - 1)
    out = []
    for i in range(n_q):
        lo = i * k
        hi = (i + 1) * k if i < n_q - 1 else len(trades)
        out.append(sum(t["pnl"] for t in trades[lo:hi]))
    return out


def split_stats(trades, is_frac=0.7):
    if not trades:
        return {"is": cb.stats([]), "oos": cb.stats([])}
    cut = int(len(trades) * is_frac)
    return {"is": cb.stats(trades[:cut]), "oos": cb.stats(trades[cut:])}


def run(arr, base_cfg, label, lev, entry_type, trend_filter_1h):
    cfg = replace(base_cfg, entry_type=entry_type, trend_filter_1h=trend_filter_1h)
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    return {
        "label": label,
        "entry_type": entry_type,
        "filter_1h": trend_filter_1h,
        "n": full["n"], "pf": full["pf"], "pnl": full["pnl"], "wr": full.get("wr"),
        "dd": full.get("dd"),
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "quarts_pos": sum(1 for q in quarts if q > 0),
        "quartiles": quarts,
    }


def grade(v):
    if v["n"] < 15:
        return "small"
    if v["pnl"] <= 0:
        return "unprofitable"
    if v["pf"] is None or v["pf"] < 1.1:
        return "PF<1.1"
    if v["oos_pf"] is None or v["oos_pf"] < 1.0:
        return "OOS<1.0"
    if v["quarts_pos"] < 3:
        return "quartiles"
    return "PASS"


def main():
    print(f"[1/3] Fetching candles for {len(UNIVERSE)} symbols "
          f"(15m×4000 + 1h×2000 + 4h×1000 each, 4h disk cache)")
    arrs = {}
    skipped = []
    for sym in UNIVERSE:
        try:
            d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
            d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
            d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
        except Exception as e:
            print(f"   {sym:<12} fetch failed: {e}")
            skipped.append((sym, f"fetch: {e}"))
            continue
        if len(d15) < 500:
            print(f"   {sym:<12} 15m={len(d15)} — too little data, skip")
            skipped.append((sym, f"only {len(d15)} 15m bars"))
            continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<12} 15m={len(d15)} ({days}d)  1h={len(d1h)}  4h={len(d4h)}")
        arrs[sym] = arr

    print(f"\n[2/3] Running {len(ENTRIES)*len(REGIME_FILTERS)} variants "
          f"× {len(arrs)} symbols")

    base = _base_cfg()
    all_results = {}
    for sym, arr in arrs.items():
        lev = _lev_for(sym)
        variants = []
        for et in ENTRIES:
            for f1h in REGIME_FILTERS:
                label = f"{et[:6]}+{f1h[:3]}"
                v = run(arr, base, label, lev, et, f1h)
                v["grade"] = grade(v)
                variants.append(v)
        all_results[sym] = variants
        # Print compact per-symbol table
        print(f"\n=== {sym} (lev {lev:.2f}x) ===")
        print(f"  {'VARIANT':<20} {'n':>4} {'PF':>6} {'$':>8} {'OOS PF':>8} {'Q+':>3}  grade")
        for v in variants:
            pf = f"{v['pf']:.2f}" if v['pf'] else "—"
            oos = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
            print(f"  {v['label']:<20} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {oos:>8} "
                  f"{v['quarts_pos']:>3}  {v['grade']}")

    # Summary — universe screen
    print(f"\n{'='*100}")
    print(f"  UNIVERSE SCREEN — PASSING SYMBOLS (BOS + regime confluence)")
    print(f"{'='*100}")
    print(f"{'SYM':<10} {'BEST VARIANT':<22} {'n':>4} {'PF':>6} {'OOS PF':>8} {'Q+':>3} {'P&L':>8}")
    passing_count = 0
    total_pnl = 0.0
    per_sym_best = {}
    for sym, vs in all_results.items():
        passing = [v for v in vs if v["grade"] == "PASS"]
        if passing:
            best = max(passing, key=lambda v: v["pnl"])
            oos = f"{best['oos_pf']:.2f}" if best['oos_pf'] else "—"
            pf = f"{best['pf']:.2f}" if best['pf'] else "—"
            print(f"{sym:<10} {best['label']:<22} {best['n']:>4} {pf:>6} {oos:>8} "
                  f"{best['quarts_pos']:>3} ${best['pnl']:>+6.0f}")
            passing_count += 1
            total_pnl += best["pnl"]
            per_sym_best[sym] = best

    print(f"\n{'='*100}")
    print(f"  FAILED — no variant cleared the scorecard")
    print(f"{'='*100}")
    for sym, vs in all_results.items():
        if not any(v["grade"] == "PASS" for v in vs):
            # Show the "least-bad" variant for context
            best_try = max(vs, key=lambda v: (v["pnl"], v["n"]))
            print(f"  {sym:<10} best try: {best_try['label']:<20} "
                  f"n={best_try['n']:>4} $={best_try['pnl']:>+6.0f} "
                  f"pf={best_try['pf']} grade={best_try['grade']}")

    print(f"\nUNIVERSE: {passing_count}/{len(arrs)} symbols pass. "
          f"Aggregate best-per-symbol P&L = ${total_pnl:+.0f}")
    if skipped:
        print(f"Skipped during fetch: {[s[0] for s in skipped]}")

    out_path = "/tmp/bos_regime_universe_test.json"
    with open(out_path, "w") as f:
        json.dump({
            "universe": UNIVERSE,
            "results": {s: [dict(v) for v in vs] for s, vs in all_results.items()},
            "best_per_symbol": {s: dict(v) for s, v in per_sym_best.items()},
            "skipped": skipped,
            "passing_count": passing_count,
            "total_pnl": total_pnl,
        }, f, indent=2, default=str)
    print(f"\nFull → {out_path}")


if __name__ == "__main__":
    main()
