"""±20% sensitivity check on the three proposed ladder upgrades before
config deployment. Edge must hold PF ≥ 1.0 at sl/tp × 0.8 AND × 1.2.

Candidates:
  BTC    → FARTCOIN ladder (TP1 3.0×30%, TP2 4.5×30%, TP3 6.0×20%, trail 2.5)
  ETH    → SILVER ladder   (TP1 2.0×30%, TP2 3.0×30%, TP3 4.0×20%, trail 0)
  PENDLE → SILVER ladder   (same as ETH)
"""
from __future__ import annotations

import os, sys, json
from dataclasses import replace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb
import research.commod_oos as oos
from research.ensemble_regime_test import _cfg_from_deployed, _patch_weekday


PROPOSALS = {
    "BTC":    {"label": "FARTCOIN_LADDER",
               "tp1": (3.0, 0.30), "tp2": (4.5, 0.30), "tp3": (6.0, 0.20),
               "trail": 2.5},
    "ETH":    {"label": "SILVER_LADDER",
               "tp1": (2.0, 0.30), "tp2": (3.0, 0.30), "tp3": (4.0, 0.20),
               "trail": 0.0},
    "PENDLE": {"label": "SILVER_LADDER",
               "tp1": (2.0, 0.30), "tp2": (3.0, 0.30), "tp3": (4.0, 0.20),
               "trail": 0.0},
}


def build_cfg(dep, proposal):
    base = _cfg_from_deployed(dep)
    return replace(base,
                   ensemble_k=int(dep.get("ensemble_k", 4)),
                   require_bos_confirm=bool(dep.get("require_bos_confirm", False)),
                   max_hold_bars=1000,
                   tp1_atr=proposal["tp1"][0], tp1_pct=proposal["tp1"][1],
                   tp2_atr=proposal["tp2"][0], tp2_pct=proposal["tp2"][1],
                   tp3_atr=proposal["tp3"][0], tp3_pct=proposal["tp3"][1],
                   trail_atr=proposal["trail"])


def main():
    dep_all = load_all()
    results = {}
    for sym, prop in PROPOSALS.items():
        dep = dep_all[sym]
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        cfg = build_cfg(dep, prop)

        base_trades = cb.backtest(arr, cfg, lev)
        base_stats = cb.stats(base_trades)
        sens = oos.sensitivity(arr, cfg, lev)

        print(f"\n=== {sym}  ({prop['label']}) ===")
        print(f"  baseline mult=1.0:  n={base_stats['n']} pnl=${base_stats['pnl']:+.0f} "
              f"pf={base_stats['pf']} wr={base_stats['wr']}%")
        for s in sens:
            broken = (s['pf'] or 0) < 1.0
            flag = " ← BROKEN" if broken else ""
            print(f"  sens     mult={s['mult']}:  n={s['n']} pnl=${s['pnl']:+.0f} "
                  f"pf={s['pf']} wr={s['wr']}%{flag}")

        all_hold = all((s["pf"] or 0) >= 1.0 for s in sens)
        print(f"  → {'PASS ✓' if all_hold else 'FAIL ✗'} (need PF≥1.0 on both ±20% perturbations)")
        results[sym] = {
            "proposal": prop,
            "baseline": base_stats,
            "sensitivity": sens,
            "pass": all_hold,
        }

    print(f"\n{'='*72}")
    print(f"  VERDICT")
    print(f"{'='*72}")
    for sym, r in results.items():
        mark = "PASS" if r["pass"] else "FAIL"
        lo = next(s for s in r["sensitivity"] if s["mult"] == 0.8)
        hi = next(s for s in r["sensitivity"] if s["mult"] == 1.2)
        print(f"  {sym:<8} {mark:<4}  PF: base={r['baseline']['pf']}  "
              f"×0.8={lo['pf']}  ×1.2={hi['pf']}")

    with open("/tmp/tp_ladder_sensitivity.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull → /tmp/tp_ladder_sensitivity.json")


if __name__ == "__main__":
    main()
