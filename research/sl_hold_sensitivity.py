"""Parameter sensitivity: wider SLs and hold-longer variants vs current configs.

For every currently-deployed symbol, holds the elected config fixed and varies:
  - sl_atr × 1.5 / × 2.0           (wider stops — give trades more room)
  - trail_atr × 1.5                (wider trail — let winners run further)
  - max_hold_bars × 2              (don't time out as fast)
  - TP ladder × 1.25               (push targets further out — hold longer)
  - sl × 1.5 + trail × 1.5         (combined: wider stops + wider trail)

Output: per-symbol comparison table + aggregate.
"""
from __future__ import annotations
import os, sys, json, copy
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
from config.deployer import load_all
import research.commod_backtest as cb
from research.commod_backtest import Cfg, fetch_hl, add_features, precompute, backtest, stats
from research.intensive_grid import hl_max_leverage


def cfg_from_deployed(d):
    return Cfg(
        trend_filter=d.get("trend_filter", "ema_slope"),
        entry_type=d["entry_type"],
        rsi_oversold=float(d["rsi_oversold"]),
        rsi_overbought=float(d["rsi_overbought"]),
        sl_atr=float(d["sl_atr"]),
        tp1_atr=float(d.get("tp1_atr", 0)),
        tp1_pct=float(d.get("tp1_pct", 0)),
        tp2_atr=float(d.get("tp2_atr", 0)),
        tp2_pct=float(d.get("tp2_pct", 0)),
        tp3_atr=float(d.get("tp3_atr", 0)),
        tp3_pct=float(d.get("tp3_pct", 0)),
        trail_atr=float(d.get("trail_atr", 0)),
        max_hold_bars=int(d.get("max_hold_bars", 1000)),
        direction=d.get("direction", "both"),
        use_1h_filter=bool(d.get("use_1h_filter", False)),
        trend_filter_1h=d.get("trend_filter_1h", "ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement", False)),
        ensemble_k=int(d.get("ensemble_k", 4)),
        require_bos_confirm=bool(d.get("require_bos_confirm", False)),
        exit_type=d.get("exit_type", "standard"),
    )


def fetch_arr(sym):
    try:
        d15 = add_features(fetch_hl(sym, "15m", 4000))
        d1h = add_features(fetch_hl(sym, "1h", 2000))
        d4h = add_features(fetch_hl(sym, "4h", 1000))
    except Exception as e:
        return None
    if len(d15) < 500 or len(d1h) < 100:
        return None
    arr = precompute(d15, d1h, d4h)
    if not sym.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
    return arr


def make_variants(base: Cfg):
    """Yield (label, cfg) variants. SL=0 is sentinel for entry types that use
    flat-% SL (test_bounce, pullback_in_regime) — for those, we can still scale
    via sl_atr but the backtester will use 3% flat regardless. Skip SL variants
    in that case."""
    variants = [("baseline", base)]
    flat_sl = base.entry_type in ("test_bounce", "pullback_in_regime")

    # Wider stops (only applicable when entry uses ATR-based SL)
    if not flat_sl and base.sl_atr > 0:
        variants.append(("sl×1.5", replace(base, sl_atr=base.sl_atr * 1.5)))
        variants.append(("sl×2.0", replace(base, sl_atr=base.sl_atr * 2.0)))

    # Wider trail (only if trail is active in baseline)
    if base.trail_atr > 0:
        variants.append(("trail×1.5", replace(base, trail_atr=base.trail_atr * 1.5)))

    # Hold longer
    variants.append(("max_hold×2", replace(base, max_hold_bars=base.max_hold_bars * 2)))

    # Wider TP ladder (let winners run)
    if base.tp1_atr > 0 or base.tp2_atr > 0 or base.tp3_atr > 0:
        variants.append(("tp×1.25", replace(base,
            tp1_atr=base.tp1_atr * 1.25,
            tp2_atr=base.tp2_atr * 1.25,
            tp3_atr=base.tp3_atr * 1.25,
        )))

    # Combined wider-stop + hold-longer
    if not flat_sl and base.sl_atr > 0 and base.trail_atr > 0:
        variants.append(("sl×1.5+trail×1.5", replace(base,
            sl_atr=base.sl_atr * 1.5,
            trail_atr=base.trail_atr * 1.5,
        )))

    return variants


def run_symbol(sym, deployed_cfg, lev, arr):
    base = cfg_from_deployed(deployed_cfg)
    rows = []
    for label, cfg in make_variants(base):
        try:
            trades = backtest(arr, cfg, lev)
        except Exception as e:
            rows.append({"label": label, "n": 0, "pnl": 0, "pf": None, "wr": 0, "err": str(e)})
            continue
        s = stats(trades)
        # Track 70/30 OOS for context
        n = len(arr["close"])
        oos_cut = arr["timestamp"][int(n * 0.7)]
        oos_trades = [t for t in trades if t["ts"] >= oos_cut]
        oos_s = stats(oos_trades)
        rows.append({
            "label": label, "n": s["n"], "pnl": s["pnl"], "pf": s["pf"],
            "wr": s.get("wr"),
            "oos_n": oos_s["n"], "oos_pnl": oos_s["pnl"], "oos_pf": oos_s["pf"],
        })
    return rows


def main():
    deployed = load_all()
    print(f"Loaded {len(deployed)} deployed configs: {sorted(deployed.keys())}\n")

    all_results = {}
    for sym in sorted(deployed.keys()):
        print(f"--- {sym} ---")
        arr = fetch_arr(sym)
        if arr is None:
            print(f"   skip: no data")
            continue
        try:
            lev = hl_max_leverage(sym) * 0.15
        except Exception:
            lev = 5 * 0.15
        rows = run_symbol(sym, deployed[sym], lev, arr)
        all_results[sym] = rows
        baseline_pnl = next((r["pnl"] for r in rows if r["label"] == "baseline"), 0)
        for r in rows:
            pf_s = f"{r['pf']:.2f}" if r['pf'] else "—"
            wr_s = f"{r['wr']:.0f}" if r['wr'] is not None else "—"
            oos_pf = f"{r.get('oos_pf'):.2f}" if r.get('oos_pf') else "—"
            delta = r["pnl"] - baseline_pnl
            d_str = f"({delta:+.0f})" if r["label"] != "baseline" else ""
            print(f"  {r['label']:<22} n={r['n']:>3}  PF={pf_s:>5}  ${r['pnl']:>+5.0f} {d_str:<8}  "
                  f"OOS n={r.get('oos_n',0):>2} ${r.get('oos_pnl',0):>+5.0f} pf={oos_pf:>4}")
        print()

    # Aggregate per-variant
    print("=" * 90)
    print("AGGREGATE — total $PnL across all symbols per variant")
    print("=" * 90)
    variant_totals = {}
    for sym, rows in all_results.items():
        for r in rows:
            v = variant_totals.setdefault(r["label"], {"pnl": 0, "oos_pnl": 0, "n": 0, "oos_n": 0, "syms": 0})
            v["pnl"] += r["pnl"]
            v["oos_pnl"] += r.get("oos_pnl", 0)
            v["n"] += r["n"]
            v["oos_n"] += r.get("oos_n", 0)
            v["syms"] += 1
    print(f"{'variant':<22} {'syms':>5} {'trades':>7} {'$PnL':>8} {'$Δ vs base':>12}  {'OOS n':>6} {'OOS $':>8}")
    base_total = variant_totals.get("baseline", {}).get("pnl", 0)
    for v_label, v in variant_totals.items():
        delta = v["pnl"] - base_total
        d_str = f"{delta:+.0f}" if v_label != "baseline" else "—"
        print(f"  {v_label:<20} {v['syms']:>5} {v['n']:>7} ${v['pnl']:>+5.0f}  ${d_str:>10}   "
              f"{v['oos_n']:>6} ${v['oos_pnl']:>+6.0f}")

    out = "/tmp/sl_hold_sensitivity.json"
    with open(out, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
