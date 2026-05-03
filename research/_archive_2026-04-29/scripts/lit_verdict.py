"""LIT make-or-break verdict.

Runs the deployed whale_LIT config through the full commod_backtest framework
on all available HL data, then grades it using the scorecard gates from the
project CLAUDE.md:

    PASS if full PF >= 1.1 AND OOS PF >= 1.0 AND >= 3/4 quartiles positive
    FAIL otherwise — candidate for retirement

Also sweeps alternative filters + directions to see if ANY variant of LIT is
saveable, or if the symbol is structurally broken across the board.
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

SYM = "LIT"


def q_pnls(trades, n_q=4):
    if not trades: return [0.0] * n_q
    k = len(trades) // n_q
    if k == 0: return [sum(t["pnl"] for t in trades)] + [0.0] * (n_q - 1)
    out = []
    for i in range(n_q):
        lo = i * k; hi = (i + 1) * k if i < n_q - 1 else len(trades)
        out.append(sum(t["pnl"] for t in trades[lo:hi]))
    return out


def split_stats(trades, is_frac=0.7):
    if not trades:
        return {"is": cb.stats([]), "oos": cb.stats([])}
    cut = int(len(trades) * is_frac)
    return {"is": cb.stats(trades[:cut]), "oos": cb.stats(trades[cut:])}


def run_variant(arr, base_cfg, label, **overrides):
    cfg = replace(base_cfg, **overrides)
    lev = INSTRUMENTS[SYM].hl_max_leverage * 0.15
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    quarts_positive = sum(1 for q in quarts if q > 0)
    return {
        "label": label, "n": full["n"], "pnl": full["pnl"], "pf": full["pf"], "wr": full.get("wr"),
        "is_n": split["is"]["n"], "is_pnl": split["is"]["pnl"], "is_pf": split["is"]["pf"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "quartiles": [round(q, 2) for q in quarts], "quartiles_positive": quarts_positive,
    }


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


def grade(v):
    """Scorecard: PASS if PF>=1.1 AND OOS PF>=1.0 AND quartiles_positive>=3 AND n>=20"""
    if v["n"] < 20: return "INSUFFICIENT"
    if v["pf"] is None or v["pf"] < 1.1: return "FAIL (low PF)"
    if v["oos_pf"] is None or v["oos_pf"] < 1.0: return "FAIL (OOS)"
    if v["quartiles_positive"] < 3: return "FAIL (quartiles unstable)"
    return "PASS"


def main():
    deployed = load_all()
    if SYM not in deployed:
        print(f"No deployed config for {SYM}"); return
    d = deployed[SYM]
    print(f"LIT current deployed config:")
    print(f"  direction={d['direction']}  entry={d['entry_type']}  1h_filter={d.get('trend_filter_1h')}")
    print(f"  4h_agreement={d.get('require_4h_agreement', False)}  "
          f"btc_confirm={d.get('require_btc_1h_confirm', False)}")

    print(f"\nFetching LIT data...")
    d15 = cb.add_features(cb.fetch_hl(SYM, "15m", 4000))
    d1h = cb.add_features(cb.fetch_hl(SYM, "1h", 2000))
    d4h = cb.add_features(cb.fetch_hl(SYM, "4h", 1000))
    days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
    print(f"  15m={len(d15)} bars ({days} days)  1h={len(d1h)}  4h={len(d4h)}")

    arr = cb.precompute(d15, d1h, d4h)
    arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)

    base_cfg = _cfg_from_deployed(d)

    variants = []
    variants.append(run_variant(arr, base_cfg, "current deployed"))
    # Direction alternatives
    variants.append(run_variant(arr, base_cfg, "long_only + hma", trend_filter_1h="hma_slope", direction="long_only"))
    variants.append(run_variant(arr, base_cfg, "short_only + hma", trend_filter_1h="hma_slope", direction="short_only"))
    variants.append(run_variant(arr, base_cfg, "short_only + sjm", trend_filter_1h="sjm", direction="short_only"))
    variants.append(run_variant(arr, base_cfg, "short_only + structure + 4h", trend_filter_1h="structure",
                                direction="short_only", require_4h_agreement=True))
    variants.append(run_variant(arr, base_cfg, "both + structure + 4h", trend_filter_1h="structure",
                                direction="both", require_4h_agreement=True))
    # Retire test — just the scorecard on current
    print(f"\n{'='*115}")
    print(f"{'VARIANT':<34} {'n':>4} {'PF':>6} {'$P&L':>8} {'WR%':>5} "
          f"{'IS PF':>6} {'OOS n':>6} {'OOS$':>8} {'OOS PF':>7} {'Q+':>4}  VERDICT")
    print('='*115)
    for v in variants:
        pf = f"{v['pf']:.2f}" if v['pf'] else "—"
        wr = f"{v['wr']:.0f}" if v['wr'] is not None else "—"
        is_pf = f"{v['is_pf']:.2f}" if v['is_pf'] else "—"
        oos_pf = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
        g = grade(v)
        print(f"{v['label']:<34} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {wr:>5} "
              f"{is_pf:>6} {v['oos_n']:>6} ${v['oos_pnl']:>+6.0f} {oos_pf:>7} {v['quartiles_positive']:>4}  {g}")

    print(f"\nQuartile P&L by variant (equal-trade-count quarters):")
    for v in variants:
        q_str = " ".join(f"${q:>+6.0f}" for q in v["quartiles"])
        print(f"  {v['label']:<34} {q_str}")

    # Final recommendation
    passing = [v for v in variants if grade(v) == "PASS"]
    print(f"\n{'='*60}\nFINAL VERDICT on LIT:")
    current = variants[0]
    g_current = grade(current)
    print(f"  Current deployed config: {g_current}")
    if g_current == "PASS":
        print(f"  → KEEP LIT. Current config passes scorecard.")
    elif passing:
        best = max(passing, key=lambda v: v["pnl"])
        print(f"  → Current FAILS, but {best['label']!r} PASSES.")
        print(f"     Consider swapping: PF {best['pf']:.2f}, OOS PF {best['oos_pf']:.2f}, "
              f"{best['quartiles_positive']}/4 quartiles positive, ${best['pnl']:+.0f}")
    else:
        print(f"  → No variant passes scorecard. RETIRE LIT from live symbol list.")

    with open("/tmp/lit_verdict.json", "w") as f:
        json.dump(variants, f, indent=2, default=str)
    print(f"\nFull → /tmp/lit_verdict.json")


if __name__ == "__main__":
    main()
