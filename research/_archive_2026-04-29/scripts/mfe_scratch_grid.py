"""MFE scratch-exit grid sweep — Lever 3 from 2026-04-27 loss analysis.

The backtest already has an MFE-based time-stop:
   commod_backtest.TIME_STOP_STALE_BARS = 16
   commod_backtest.TIME_STOP_MIN_MFE_ATR = 0.3

Post-hoc analysis on 76 paper trades suggested tighter params (4-8 bars,
0.3-0.8 ATR) would catch ~5 of the worst losers (incl. today's ZEC -$489)
and convert ~$215 → ~$700 realized PnL — but on a single-sample post-hoc.

This script:
  1. Runs all live symbols at baseline (16 / 0.3) and grid (4-12 / 0.3-0.8)
  2. Aggregates total PnL across symbols per variant
  3. Per-symbol IS/OOS split (70/30) for the top variants
  4. Quartile stability check
  5. Reports W/L breakdown of trades that get scratched vs ride-out

Output: ranked grid + per-symbol detail for top 3 candidates → /tmp/mfe_scratch_grid.json
"""
from __future__ import annotations

import os
import sys
import json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb

LIVE_SYMBOLS = ["BTC", "HYPE", "ZEC", "XRP", "FARTCOIN", "LIT", "ENA", "SOL",
                "ARB", "OP", "PENDLE", "TIA", "ETH"]


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
        ensemble_k=int(d.get("ensemble_k", 4)),
        require_bos_confirm=bool(d.get("require_bos_confirm", False)),
        exit_type=d.get("exit_type", "standard"),
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


def run_variant(arrs, cfgs, stale_bars: int, min_mfe: float, label: str):
    """Run all symbols with the given time-stop params and aggregate."""
    cb.TIME_STOP_ENABLED = True
    cb.TIME_STOP_STALE_BARS = stale_bars
    cb.TIME_STOP_MIN_MFE_ATR = min_mfe

    per_sym = {}
    all_trades = []
    n_scratched = 0
    scratched_pnl = 0.0
    for sym, arr in arrs.items():
        cfg = cfgs[sym]
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        trades = cb.backtest(arr, cfg, lev)
        st = cb.stats(trades)
        sp = split_stats(trades)
        # Count "time_stop" exits — these are the scratched trades
        ts_trades = [t for t in trades if t.get("reason") == "time_stop"]
        n_scratched += len(ts_trades)
        scratched_pnl += sum(t["pnl"] for t in ts_trades)
        per_sym[sym] = {
            "n": st["n"], "pnl": st["pnl"], "pf": st["pf"], "wr": st.get("wr"),
            "n_scratched": len(ts_trades),
            "scratched_pnl": sum(t["pnl"] for t in ts_trades),
            "is_n": sp["is"]["n"], "is_pnl": sp["is"]["pnl"], "is_pf": sp["is"]["pf"],
            "oos_n": sp["oos"]["n"], "oos_pnl": sp["oos"]["pnl"], "oos_pf": sp["oos"]["pf"],
            "quartile_pnls": q_pnls(trades, 4),
        }
        all_trades.extend(trades)

    agg = cb.stats(all_trades)
    agg_split = split_stats(all_trades)
    return {
        "label": label,
        "stale_bars": stale_bars,
        "min_mfe": min_mfe,
        "agg_n": agg["n"],
        "agg_pnl": agg["pnl"],
        "agg_pf": agg["pf"],
        "agg_wr": agg.get("wr"),
        "agg_is_n": agg_split["is"]["n"],
        "agg_is_pnl": agg_split["is"]["pnl"],
        "agg_oos_n": agg_split["oos"]["n"],
        "agg_oos_pnl": agg_split["oos"]["pnl"],
        "n_scratched": n_scratched,
        "scratched_pnl": scratched_pnl,
        "per_sym": per_sym,
    }


def main():
    import time as _time
    deployed = load_all()
    print(f"[1/3] Fetching data for {len(LIVE_SYMBOLS)} symbols...")
    arrs = {}
    cfgs = {}
    for sym in LIVE_SYMBOLS:
        if sym not in deployed:
            print(f"   {sym}: no deployed config, skip")
            continue
        if sym not in INSTRUMENTS:
            print(f"   {sym}: not in INSTRUMENTS (no leverage cap), skip")
            continue
        d15 = d1h = d4h = None
        for attempt in range(3):
            try:
                d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
                d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
                d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
                break
            except Exception as e:
                if attempt < 2:
                    _time.sleep(1.5)
                    continue
                print(f"   {sym}: fetch failed after 3 tries ({e}), skip"); break
        if d15 is None or d1h is None or d4h is None:
            continue
        if len(d15) < 500 or len(d1h) < 100:
            continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
        print(f"   {sym:<10}  15m={len(d15)} ({days}d)")
        arrs[sym] = arr
        cfgs[sym] = _cfg_from_deployed(deployed[sym])

    # Grid:
    #   STALE_BARS in {4, 6, 8, 12, 16}  (1h, 1.5h, 2h, 3h, 4h on 15m bars)
    #   MIN_MFE   in {0.3, 0.5, 0.8}     (current is 0.3)
    # Plus disabled baseline for reference
    stale_grid = [4, 6, 8, 12, 16]
    mfe_grid = [0.3, 0.5, 0.8]

    print(f"\n[2/3] Running grid: {len(stale_grid)} × {len(mfe_grid)} = "
          f"{len(stale_grid)*len(mfe_grid)} variants + 1 baseline (no time-stop)")

    results = []

    # Baseline: time-stop disabled entirely (so we see raw effect)
    cb.TIME_STOP_ENABLED = False
    base = run_variant(arrs, cfgs, stale_bars=999999, min_mfe=0.0,
                        label="BASELINE (no time-stop)")
    results.append(base)
    cb.TIME_STOP_ENABLED = True

    for stale in stale_grid:
        for mfe in mfe_grid:
            label = f"stale={stale:>2}  mfe<{mfe:.1f}"
            results.append(run_variant(arrs, cfgs, stale, mfe, label))

    # Annotate which variant is the current production setting
    for r in results:
        r["is_current"] = (r["stale_bars"] == 16 and r["min_mfe"] == 0.3)

    print(f"\n[3/3] Results — sorted by aggregated OOS PnL")
    print("="*108)
    print(f"{'VARIANT':<28} {'n':>4} {'PF':>5} {'$PnL':>8} {'WR%':>4}  "
          f"{'OOS n':>5} {'OOS$':>7} "
          f"{'#scrat':>6} {'$scrat':>8}  {'note':<14}")
    print("="*108)

    base_pnl = base["agg_pnl"]
    base_oos = base["agg_oos_pnl"]

    sorted_results = sorted(results, key=lambda r: r["agg_oos_pnl"], reverse=True)
    for r in sorted_results:
        pf = f"{r['agg_pf']:.2f}" if r['agg_pf'] else "—"
        wr = f"{r['agg_wr']:.0f}" if r['agg_wr'] is not None else "—"
        note = "← CURRENT" if r["is_current"] else ("← BASELINE" if r["label"].startswith("BASELINE") else "")
        print(f"{r['label']:<28} {r['agg_n']:>4} {pf:>5} ${r['agg_pnl']:>+6.0f} {wr:>4}  "
              f"{r['agg_oos_n']:>5} ${r['agg_oos_pnl']:>+5.0f}  "
              f"{r['n_scratched']:>6} ${r['scratched_pnl']:>+6.0f}  {note:<14}")

    print(f"\nDelta to baseline (no time-stop):")
    for r in sorted_results:
        if r["label"].startswith("BASELINE"):
            continue
        d_full = r["agg_pnl"] - base_pnl
        d_oos = r["agg_oos_pnl"] - base_oos
        flag = ""
        if d_full > 0 and d_oos > 0:
            flag = "  ✓ both positive"
        elif d_full > 0:
            flag = "  ⚠ IS+ OOS-"
        elif d_oos > 0:
            flag = "  ⚠ IS- OOS+"
        print(f"  {r['label']:<28} ΔPnL ${d_full:>+6.0f}   ΔOOS ${d_oos:>+6.0f}{flag}")

    # Per-symbol detail for top 3 by OOS
    print(f"\nPer-symbol detail — top 3 variants by OOS PnL:")
    for r in sorted_results[:3]:
        if r["label"].startswith("BASELINE"):
            continue
        print(f"\n  {r['label']} (agg OOS ${r['agg_oos_pnl']:+.0f}, scratched {r['n_scratched']} for ${r['scratched_pnl']:+.0f})")
        print(f"    {'sym':<10} {'n':>3} {'$PnL':>7} {'WR%':>4} {'#scrat':>6} {'$scrat':>8} {'OOS$':>7}")
        for sym, s in sorted(r["per_sym"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = f"{s['wr']:.0f}" if s['wr'] is not None else "—"
            print(f"    {sym:<10} {s['n']:>3} ${s['pnl']:>+5.0f} {wr:>4} "
                  f"{s['n_scratched']:>6} ${s['scratched_pnl']:>+6.0f} ${s['oos_pnl']:>+5.0f}")

    out = "/tmp/mfe_scratch_grid.json"
    with open(out, "w") as f:
        # strip per_sym trades for compactness
        json.dump([{k: v for k, v in r.items() if k != "per_sym_trades"}
                    for r in results], f, indent=2, default=str)
    print(f"\nFull results → {out}")


if __name__ == "__main__":
    main()
