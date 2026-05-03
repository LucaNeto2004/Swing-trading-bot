"""Run forward-walk validation on every active deployed config.

Single source of truth for "which configs are trustworthy right now".
Output: pass/fail × per-window stats × bootstrap CIs for each symbol.
"""
from __future__ import annotations
import os, sys, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
from config.deployer import load_all
import research.commod_backtest as cb
from research.commod_backtest import Cfg, fetch_hl, add_features, precompute
from research.forward_walk import forward_walk, load_gate
from research.intensive_grid import hl_max_leverage

cb.TIME_STOP_ENABLED = False  # config-as-deployed, time-stop is off

def cfg_from_dict(d):
    return Cfg(
        trend_filter=d.get("trend_filter","ema_slope"),
        entry_type=d["entry_type"],
        rsi_oversold=float(d.get("rsi_oversold",30)), rsi_overbought=float(d.get("rsi_overbought",70)),
        sl_atr=float(d.get("sl_atr",2.0)),
        tp1_atr=float(d.get("tp1_atr",0)), tp1_pct=float(d.get("tp1_pct",0)),
        tp2_atr=float(d.get("tp2_atr",0)), tp2_pct=float(d.get("tp2_pct",0)),
        tp3_atr=float(d.get("tp3_atr",0)), tp3_pct=float(d.get("tp3_pct",0)),
        trail_atr=float(d.get("trail_atr",0)),
        max_hold_bars=int(d.get("max_hold_bars",1000)),
        direction=d.get("direction","both"),
        use_1h_filter=bool(d.get("use_1h_filter",False)),
        trend_filter_1h=d.get("trend_filter_1h","ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement",False)),
        ensemble_k=int(d.get("ensemble_k",4)),
        require_bos_confirm=bool(d.get("require_bos_confirm",False)),
        exit_type=d.get("exit_type","standard"),
    )

deployed = load_all()
gate = load_gate()
print(f"Forward-walk validation against gate v{gate['_meta']['version']}\n")
print(f"Gate: OOS n>={gate['deployment_gate']['min_oos_n_trades']}, "
      f"PF>={gate['deployment_gate']['min_oos_pf']}, "
      f"all-quartiles-positive, "
      f"PF beats random by {gate['deployment_gate']['min_pf_above_random']}\n")

results = {}
for sym in sorted(deployed.keys()):
    print(f"\n{'='*70}\n{sym}\n{'='*70}")
    d = deployed[sym]
    try:
        d15 = add_features(fetch_hl(sym, "15m", 4000))
        d1h = add_features(fetch_hl(sym, "1h", 2000))
        d4h = add_features(fetch_hl(sym, "4h", 1000))
        if d15 is None or len(d15) < 500:
            print(f"  insufficient data, skip")
            results[sym] = {"verdict": "SKIP", "reason": "insufficient data"}
            continue
        arr = precompute(d15, d1h, d4h)
        if not sym.startswith("xyz:"):
            arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
    except Exception as e:
        print(f"  fetch error: {e}")
        results[sym] = {"verdict": "ERROR", "reason": str(e)}
        continue

    cfg = cfg_from_dict(d)
    lev = hl_max_leverage(sym) * 0.15
    r = forward_walk(cfg, arr, lev, verbose=True)
    results[sym] = r

# Final summary
print(f"\n\n{'='*70}\nFORWARD-WALK SUMMARY (gate v{gate['_meta']['version']})\n{'='*70}")
print(f"{'sym':<12} {'verdict':<8} {'win/N':>6} {'PF mean':>9} {'PF 95% CI':>20} {'$ mean':>9} {'$ 95% CI':>20}")
print('-' * 95)
for sym, r in results.items():
    if r.get("verdict") in ("SKIP", "ERROR"):
        print(f"{sym:<12} {r['verdict']:<8}  — — —  ({r.get('reason','')})")
        continue
    v = "PASS ✓" if r["verdict"]["pass"] else "FAIL ✗"
    win_n = f"{r['windows_passed']}/{r['n_windows']}"
    agg = r["aggregate"]
    pf_mean = f"{agg['pf_mean']:.2f}" if agg['pf_mean'] is not None else "—"
    pf_ci = (f"[{agg['pf_ci_95'][0]:.2f}, {agg['pf_ci_95'][1]:.2f}]"
             if agg['pf_ci_95'][0] is not None else "—")
    pnl_mean = f"${agg['pnl_mean']:+.0f}" if agg['pnl_mean'] is not None else "—"
    pnl_ci = (f"[${agg['pnl_ci_95'][0]:+.0f}, ${agg['pnl_ci_95'][1]:+.0f}]"
              if agg['pnl_ci_95'][0] is not None else "—")
    print(f"{sym:<12} {v:<8} {win_n:>6} {pf_mean:>9} {pf_ci:>20} {pnl_mean:>9} {pnl_ci:>20}")

with open("/tmp/validate_all.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
print(f"\nFull → /tmp/validate_all.json")
