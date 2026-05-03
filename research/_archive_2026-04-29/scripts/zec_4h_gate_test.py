"""ZEC 4h-agreement gate OOS test.

Held fixed: everything in the current deployed whale_ZEC.json.
Variable:    `require_4h_agreement` (False → True).

Also sweeps the 1h filter variant (current=both_agree vs hma vs sjm vs structure)
to see if a combined change (4h gate ON + different 1h filter) does even better.

Output: IS 30d / OOS 11d split + full-sample stats, plus quartile breakdown so
we can tell if edge is concentrated in one good week or spread evenly.
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

SYM = "ZEC"


def q_pnls(trades, n_q=4):
    if not trades: return [0.0] * n_q
    k = len(trades) // n_q
    if k == 0: return [sum(t["pnl"] for t in trades)] + [0.0] * (n_q - 1)
    out = []
    for i in range(n_q):
        lo = i * k
        hi = (i + 1) * k if i < n_q - 1 else len(trades)
        out.append(sum(t["pnl"] for t in trades[lo:hi]))
    return out


def split_stats(trades, total_bars, is_frac=0.7):
    """Split trades into IS (first 70%) and OOS (last 30%) by trade index."""
    if not trades:
        return {"is": cb.stats([]), "oos": cb.stats([])}
    cut = int(len(trades) * is_frac)
    is_t = trades[:cut]; oos_t = trades[cut:]
    return {"is": cb.stats(is_t), "oos": cb.stats(oos_t), "n_trades": len(trades)}


def run_variant(arr, base_cfg, label, **overrides):
    cfg = replace(base_cfg, **overrides)
    lev = INSTRUMENTS[SYM].hl_max_leverage * 0.15
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades, len(arr["close"]))
    quarts = q_pnls(trades, 4)
    return {
        "label": label,
        "n": full["n"], "pnl": full["pnl"], "pf": full["pf"], "wr": full.get("wr"),
        "max_dd": full.get("max_dd"),
        "is_n": split["is"]["n"], "is_pnl": split["is"]["pnl"], "is_pf": split["is"]["pf"],
        "oos_n": split["oos"]["n"], "oos_pnl": split["oos"]["pnl"], "oos_pf": split["oos"]["pf"],
        "quartile_pnls": [round(q, 2) for q in quarts],
    }


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


def main():
    deployed = load_all()
    if SYM not in deployed:
        print(f"No deployed config for {SYM}"); return
    d = deployed[SYM]
    print(f"ZEC current config: 1h_filter={d.get('trend_filter_1h')}  4h_agreement={d.get('require_4h_agreement', False)}")

    print(f"\nFetching ZEC data...")
    d15 = cb.add_features(cb.fetch_hl(SYM, "15m", 4000))
    d1h = cb.add_features(cb.fetch_hl(SYM, "1h", 2000))
    d4h = cb.add_features(cb.fetch_hl(SYM, "4h", 1000))
    days = (d15["timestamp"].iloc[-1] - d15["timestamp"].iloc[0]).days
    print(f"  15m={len(d15)} bars ({days} days)  1h={len(d1h)}  4h={len(d4h)}")

    arr = cb.precompute(d15, d1h, d4h)
    arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)  # crypto 24/7

    base_cfg = _cfg_from_deployed(d)

    variants = []
    # 1. Current (baseline)
    variants.append(run_variant(arr, base_cfg, "current (both_agree, no 4h)"))

    # 2. Current 1h filter + 4h ON
    variants.append(run_variant(arr, base_cfg, "both_agree + 4h ON", require_4h_agreement=True))

    # 3. HMA + no 4h
    variants.append(run_variant(arr, base_cfg, "hma_slope, no 4h", trend_filter_1h="hma_slope"))

    # 4. HMA + 4h ON
    variants.append(run_variant(arr, base_cfg, "hma_slope + 4h ON",
                                trend_filter_1h="hma_slope", require_4h_agreement=True))

    # 5. SJM + no 4h
    variants.append(run_variant(arr, base_cfg, "sjm, no 4h", trend_filter_1h="sjm"))

    # 6. SJM + 4h ON
    variants.append(run_variant(arr, base_cfg, "sjm + 4h ON",
                                trend_filter_1h="sjm", require_4h_agreement=True))

    # 7. structure only + 4h ON
    variants.append(run_variant(arr, base_cfg, "structure + 4h ON",
                                trend_filter_1h="structure", require_4h_agreement=True))

    print(f"\n{'='*105}")
    print(f"{'VARIANT':<32} {'n':>5} {'PF':>6} {'$PnL':>9} {'WR%':>6} "
          f"{'IS n':>5} {'IS$':>8} {'IS PF':>6}  {'OOS n':>6} {'OOS$':>8} {'OOS PF':>7}")
    print('='*105)
    for v in variants:
        pf = f"{v['pf']:.2f}" if v['pf'] else "—"
        wr = f"{v['wr']:.0f}" if v['wr'] is not None else "—"
        is_pf = f"{v['is_pf']:.2f}" if v['is_pf'] else "—"
        oos_pf = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
        print(f"{v['label']:<32} {v['n']:>5} {pf:>6} ${v['pnl']:>+7.0f} {wr:>6} "
              f"{v['is_n']:>5} ${v['is_pnl']:>+6.0f} {is_pf:>6}  "
              f"{v['oos_n']:>6} ${v['oos_pnl']:>+6.0f} {oos_pf:>7}")

    print(f"\nQuartile P&L (equal-trade-count quarters — stability check):")
    for v in variants:
        q_str = " ".join(f"${q:>+6.0f}" for q in v["quartile_pnls"])
        positive = sum(1 for q in v["quartile_pnls"] if q > 0)
        print(f"  {v['label']:<32} {q_str}   [{positive}/4 positive]")

    with open("/tmp/zec_4h_gate_test.json", "w") as f:
        json.dump(variants, f, indent=2, default=str)
    print(f"\nFull → /tmp/zec_4h_gate_test.json")


if __name__ == "__main__":
    main()
