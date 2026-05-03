"""ZEC 1h-filter OOS test.

Held fixed: everything in the current deployed whale_ZEC.json (entry_type
pullback_in_regime, flat 3% SL, TP ladder, long_only, no 4h gate).

Variable:    `use_1h_filter` False → True, sweeping `trend_filter_1h` across
             ema_cross / hma_slope / sjm / kalman / both_agree / structure.

Motivation: 2026-04-27 ZEC stop -$489 = -1R; pre-stop tape (TP1 partial →
runner stop → pullback exit) showed regime had already rolled over, but
strategy has no 1h trend filter so it kept buying. Same diagnosis ETH had
before its 2026-04-26 SJM swap (-$79 → +$55 over 41d head-to-head).

Output: full-sample stats + IS 70 / OOS 30 split + quartile breakdown.
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
    return {
        "is": cb.stats(trades[:cut]),
        "oos": cb.stats(trades[cut:]),
        "n_trades": len(trades),
    }


def run_variant(arr, base_cfg, label, **overrides):
    cfg = replace(base_cfg, **overrides)
    lev = INSTRUMENTS[SYM].hl_max_leverage * 0.15
    trades = cb.backtest(arr, cfg, lev)
    full = cb.stats(trades)
    split = split_stats(trades)
    quarts = q_pnls(trades, 4)
    return {
        "label": label,
        "use_1h_filter": cfg.use_1h_filter,
        "trend_filter_1h": cfg.trend_filter_1h,
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
        print(f"No deployed config for {SYM}")
        return
    d = deployed[SYM]
    print(f"ZEC current config: use_1h_filter={d.get('use_1h_filter')}  "
          f"trend_filter_1h={d.get('trend_filter_1h')}  "
          f"entry={d.get('entry_type')}  sl_atr={d.get('sl_atr')}")

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
    # 1. Baseline = current deployed (use_1h_filter=False, no filter active)
    variants.append(run_variant(arr, base_cfg, "BASELINE (no 1h filter)"))

    # 2-7. Each filter variant with use_1h_filter=True
    for f in ("ema_cross", "hma_slope", "sjm", "kalman", "both_agree", "structure"):
        variants.append(run_variant(
            arr, base_cfg, f"1h filter ON: {f}",
            use_1h_filter=True, trend_filter_1h=f,
        ))

    print(f"\n{'='*108}")
    print(f"{'VARIANT':<28} {'n':>4} {'PF':>6} {'$PnL':>8} {'WR%':>5} {'maxDD':>7}  "
          f"{'IS n':>5} {'IS$':>7} {'IS PF':>6}  {'OOS n':>6} {'OOS$':>7} {'OOS PF':>7}")
    print('=' * 108)
    base_pnl = variants[0]["pnl"]
    for v in variants:
        pf = f"{v['pf']:.2f}" if v['pf'] else "—"
        wr = f"{v['wr']:.0f}" if v['wr'] is not None else "—"
        is_pf = f"{v['is_pf']:.2f}" if v['is_pf'] else "—"
        oos_pf = f"{v['oos_pf']:.2f}" if v['oos_pf'] else "—"
        dd = f"{v['max_dd']:.0f}" if v['max_dd'] is not None else "—"
        print(f"{v['label']:<28} {v['n']:>4} {pf:>6} ${v['pnl']:>+6.0f} {wr:>5} {dd:>7}  "
              f"{v['is_n']:>5} ${v['is_pnl']:>+5.0f} {is_pf:>6}  "
              f"{v['oos_n']:>6} ${v['oos_pnl']:>+5.0f} {oos_pf:>7}")

    print(f"\n{'DELTA vs BASELINE':<28} {'$Δ':>10} {'OOS $Δ':>10}")
    for v in variants[1:]:
        d_full = v["pnl"] - base_pnl
        d_oos = v["oos_pnl"] - variants[0]["oos_pnl"]
        print(f"{v['label']:<28} ${d_full:>+8.0f}  ${d_oos:>+8.0f}")

    print(f"\nQuartile P&L (equal-trade-count quarters — stability check):")
    for v in variants:
        q_str = " ".join(f"${q:>+6.0f}" for q in v["quartile_pnls"])
        positive = sum(1 for q in v["quartile_pnls"] if q > 0)
        print(f"  {v['label']:<28} {q_str}   [{positive}/4 positive]")

    print(f"\nDecision gate (per commod_oos.py methodology):")
    print(f"  - OOS PnL must be positive AND > baseline OOS")
    print(f"  - 3+/4 quartiles positive (stability)")
    print(f"  - n_trades >= 30 ideally; flag any with <20 as low-confidence")
    print(f"  - Beats baseline by enough to justify added complexity")

    out = "/tmp/zec_1h_filter_test.json"
    with open(out, "w") as f:
        json.dump(variants, f, indent=2, default=str)
    print(f"\nFull results → {out}")


if __name__ == "__main__":
    main()
