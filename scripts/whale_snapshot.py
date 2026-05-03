"""Fresh whale snapshot + recent-fills analysis for 58bro + nervousdegen.

Shows current open positions, account value, and parses recent fills to estimate
hold times, symbol concentration, and side bias — the metrics that matter for
deciding whether our bot's posture matches or diverges from theirs."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
_SHARED = os.path.join(_BASE, "shared")
if not os.path.isdir(_SHARED):
    _SHARED = os.path.abspath(os.path.join(_BASE, "..", "shared"))
sys.path.insert(0, _SHARED)

import hl_client  # noqa: E402
from research.whale_watcher import WHALE_WATCHLIST  # noqa: E402


def summarize_fills(fills: list[dict]) -> dict:
    """Group recent fills into inferred round-trips by symbol + side.
    HL fills are per-trade, so we reconstruct by summing signed size until it
    crosses zero = round trip closed."""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        by_sym[f["coin"]].append(f)

    total_notional = 0.0
    total_pnl = 0.0
    total_fees = 0.0
    side_counts = {"long": 0, "short": 0}
    avg_hold_minutes_list: list[float] = []

    for sym, sym_fills in by_sym.items():
        sym_fills = sorted(sym_fills, key=lambda x: x["timestamp_ms"])
        running = 0.0
        open_ts = None
        open_side = None
        for f in sym_fills:
            sz = f["size"]
            px = f["price"]
            notional = abs(sz) * px
            total_notional += notional
            total_pnl += f.get("closed_pnl", 0.0)
            total_fees += f.get("fee", 0.0)

            if running == 0:
                open_ts = f["timestamp_ms"]
                open_side = "long" if sz > 0 else "short"
                side_counts[open_side] += 1
            running += sz
            if abs(running) < 1e-9 and open_ts is not None:
                close_ts = f["timestamp_ms"]
                hold_min = (close_ts - open_ts) / 1000 / 60
                avg_hold_minutes_list.append(hold_min)
                open_ts = None
                open_side = None

    return {
        "fills_count": len(fills),
        "symbols": sorted(by_sym.keys()),
        "total_notional_usd": total_notional,
        "closed_pnl_usd": total_pnl,
        "fees_usd": total_fees,
        "long_opens": side_counts["long"],
        "short_opens": side_counts["short"],
        "round_trips_closed": len(avg_hold_minutes_list),
        "avg_hold_minutes": (sum(avg_hold_minutes_list) / len(avg_hold_minutes_list))
                            if avg_hold_minutes_list else 0.0,
        "median_hold_minutes": sorted(avg_hold_minutes_list)[len(avg_hold_minutes_list)//2]
                               if avg_hold_minutes_list else 0.0,
    }


def dump(name: str, addr: str):
    print(f"\n{'='*70}")
    print(f"  {name}  ({addr[:10]}…{addr[-4:]})")
    print('='*70)

    bal = hl_client.sync_get_balance(addr)
    positions = hl_client.sync_get_positions(addr)
    fills = hl_client.sync_get_recent_fills(addr, limit=200)

    print(f"\nAccount value: ${bal['account_value']:,.0f}")
    print(f"Withdrawable:  ${bal.get('withdrawable', 0):,.0f}")
    print(f"Margin used:   ${bal.get('total_margin_used', 0):,.0f}")

    if positions:
        print(f"\nOpen positions ({len(positions)}):")
        for p in positions:
            notional = abs(p['size']) * p['entry_price']
            lev = p.get('leverage', {})
            lev_val = lev.get('value', lev) if isinstance(lev, dict) else lev
            unr_pct = (p['unrealised_pnl'] / notional * 100) if notional else 0
            print(f"  {p['coin']:<10} {p['side']:>5} sz={p['size']:+,.4f}  "
                  f"entry=${p['entry_price']:.4f}  notional=${notional:,.0f}  "
                  f"lev={lev_val}x  unrl=${p['unrealised_pnl']:+,.0f} ({unr_pct:+.1f}%)  "
                  f"liq=${p['liquidation_price']:.4f}")
    else:
        print("\nNo open positions (flat).")

    if fills:
        s = summarize_fills(fills)
        print(f"\nRecent {s['fills_count']} fills:")
        print(f"  symbols traded:    {', '.join(s['symbols'])}")
        print(f"  round trips closed: {s['round_trips_closed']}")
        print(f"  long opens: {s['long_opens']}   short opens: {s['short_opens']}")
        if s['round_trips_closed']:
            print(f"  avg hold:    {s['avg_hold_minutes']/60:.1f} hours ({s['avg_hold_minutes']:.0f} min)")
            print(f"  median hold: {s['median_hold_minutes']/60:.1f} hours ({s['median_hold_minutes']:.0f} min)")
        print(f"  total notional traded: ${s['total_notional_usd']:,.0f}")
        print(f"  closed P&L (in these fills): ${s['closed_pnl_usd']:+,.0f}")
        print(f"  fees paid: ${s['fees_usd']:,.0f}")
        if fills:
            first_ts = min(f['timestamp_ms'] for f in fills)
            last_ts = max(f['timestamp_ms'] for f in fills)
            span_hours = (last_ts - first_ts) / 1000 / 3600
            print(f"  fills span: {span_hours:.1f} hours "
                  f"(from {datetime.fromtimestamp(first_ts/1000, tz=timezone.utc).strftime('%m-%d %H:%M')} UTC "
                  f"to {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).strftime('%m-%d %H:%M')} UTC)")


def main():
    for name, addr in WHALE_WATCHLIST:
        try:
            dump(name, addr)
        except Exception as e:
            print(f"\n{name} ({addr}): failed — {e}")


if __name__ == "__main__":
    main()
