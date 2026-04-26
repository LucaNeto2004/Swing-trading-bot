"""Rolling per-symbol profit factor from live paper trades.

Reads data/paper_state.json trade_history, computes 7d / 30d / all-time
PF per symbol, plus win rate and trade count. Flags symbols that look
structurally broken (PF<1.0 with sufficient trades) vs still in-sample.

Use this as the data-driven retirement criterion for any symbol. Don't
retire on single bad days — retire on rolling-window evidence.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_STATE = os.path.join(_BASE, "data", "paper_state.json")
MIN_TRADES_FOR_VERDICT = 20  # below this, "insufficient sample"


def parse_ts(ts: str) -> datetime:
    # trade_history timestamps may have microseconds, may not
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    raise ValueError(f"cannot parse timestamp {ts!r}")


def pf_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "pf": None, "wr": None, "total_pnl": 0.0,
                "gross_win": 0.0, "gross_loss": 0.0, "avg_pnl": 0.0}
    pnls = [t.get("pnl", 0.0) for t in trades]
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else None)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(trades),
        "pf": pf,
        "wr": wins / len(trades) * 100 if trades else None,
        "total_pnl": sum(pnls),
        "gross_win": gw,
        "gross_loss": gl,
        "avg_pnl": sum(pnls) / len(trades),
    }


def filter_window(trades: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=days)
    return [t for t in trades if parse_ts(t["timestamp"]) >= cutoff]


def main():
    if not os.path.exists(PAPER_STATE):
        print(f"missing {PAPER_STATE}"); sys.exit(1)
    with open(PAPER_STATE) as f:
        state = json.load(f)
    trade_history = state.get("trade_history", [])

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trade_history:
        by_sym[t.get("symbol", "?")].append(t)

    print(f"Paper trade_history: {len(trade_history)} records across {len(by_sym)} symbols")
    print(f"Balance: ${state.get('balance', 0):,.2f}  Starting: ${state.get('starting_balance', 0):,.2f}")
    print(f"All-time P&L: ${state.get('balance', 0) - state.get('starting_balance', 0):+,.2f}")

    print(f"\n{'='*98}")
    print(f"{'SYMBOL':<10} {'7d':<25} {'30d':<25} {'ALL-TIME':<25} VERDICT")
    print('='*98)

    lines = []
    for sym in sorted(by_sym.keys()):
        trades = by_sym[sym]
        s7 = pf_stats(filter_window(trades, 7))
        s30 = pf_stats(filter_window(trades, 30))
        sA = pf_stats(trades)

        def fmt(s):
            if s["n"] == 0:
                return "—"
            pf = f"{s['pf']:.2f}" if s["pf"] and s["pf"] != float("inf") else ("∞" if s["pf"] == float("inf") else "—")
            wr = f"{s['wr']:.0f}%" if s["wr"] is not None else "?"
            return f"n={s['n']:>2} PF={pf} WR={wr} ${s['total_pnl']:+,.0f}"

        # Verdict based on all-time (for now — later 30d once sample is big)
        verdict = ""
        if sA["n"] < MIN_TRADES_FOR_VERDICT:
            verdict = f"in-sample ({MIN_TRADES_FOR_VERDICT - sA['n']} more trades needed)"
        elif sA["pf"] is None or sA["pf"] < 1.0:
            verdict = "⚠ BELOW PF 1.0 — candidate for retirement"
        elif sA["pf"] < 1.2:
            verdict = "marginal (PF 1.0-1.2)"
        else:
            verdict = "ok"

        row = f"{sym:<10} {fmt(s7):<25} {fmt(s30):<25} {fmt(sA):<25} {verdict}"
        lines.append((sA["n"], row))

    # Sort by trade count descending
    for _, row in sorted(lines, key=lambda x: -x[0]):
        print(row)

    # Summary of flags
    flagged = [s for s in by_sym if pf_stats(by_sym[s])["n"] >= MIN_TRADES_FOR_VERDICT
               and (pf_stats(by_sym[s])["pf"] is None or pf_stats(by_sym[s])["pf"] < 1.0)]
    if flagged:
        print(f"\n⚠ Symbols below PF 1.0 with >={MIN_TRADES_FOR_VERDICT} trades: {flagged}")
    else:
        print(f"\nNo symbols currently flagged for retirement (all either in-sample or PF>=1.0)")


if __name__ == "__main__":
    main()
