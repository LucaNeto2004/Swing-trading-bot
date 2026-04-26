"""Trace back 58bro's BTC and ETH short positions to find the actual opening
fills, then replay our bot's filters against the 5m/1h/4h data at that moment
to see whether we would have taken the same trade."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import httpx
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from core.data import fetch_candles
from core.features import add_features, trend_lookup_1h, structure_lookup_1h, hma_slope_lookup_1h, sjm_lookup_1h

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
WHALE_58BRO = "0x418AA6Bf98a2b2BC93779f810330d88cDe488888"


def fetch_fills_by_time(addr: str, start_ms: int, end_ms: int) -> list[dict]:
    """Pull fills in [start_ms, end_ms] via userFillsByTime. Paginates by
    shrinking the window from the right as HL caps per-response count."""
    import time
    out: list[dict] = []
    cur_end = end_ms
    for _ in range(30):
        batch = None
        for attempt in range(5):
            r = httpx.post(HL_INFO_URL, json={
                "type": "userFillsByTime",
                "user": addr,
                "startTime": start_ms,
                "endTime": cur_end,
            }, timeout=20)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"  rate-limited, waiting {wait}s...")
                time.sleep(wait); continue
            r.raise_for_status()
            batch = r.json() or []
            break
        if batch is None:
            raise RuntimeError("rate-limit persistent")
        if not batch:
            break
        out.extend(batch)
        # Move end to just before the oldest fill in this batch
        oldest = min(int(f["time"]) for f in batch)
        if oldest <= start_ms or len(batch) < 10:
            break
        cur_end = oldest - 1
    # De-dupe by tid
    seen = set()
    uniq = []
    for f in sorted(out, key=lambda x: int(x["time"])):
        tid = f.get("tid")
        if tid in seen:
            continue
        seen.add(tid)
        uniq.append(f)
    return uniq


def reconstruct_position_history(fills: list[dict], coin: str) -> list[dict]:
    """Walk fills for one coin and emit (timestamp, running_size, side) at every change.
    Returns list of position-state snapshots."""
    sym_fills = [f for f in fills if f.get("coin") == coin]
    sym_fills.sort(key=lambda x: int(x["time"]))
    running = 0.0
    states = []
    for f in sym_fills:
        sz = float(f["sz"])
        signed = sz if f["side"] == "B" else -sz
        prev = running
        running += signed
        states.append({
            "ts_ms": int(f["time"]),
            "dt": datetime.fromtimestamp(int(f["time"]) / 1000, tz=timezone.utc),
            "prev_size": prev,
            "delta": signed,
            "new_size": running,
            "price": float(f["px"]),
            "closed_pnl": float(f.get("closedPnl", 0)),
        })
    return states


def find_short_opens(states: list[dict]) -> list[dict]:
    """Return fills where running size crossed zero into short, or added to short."""
    opens = []
    for s in states:
        if s["prev_size"] == 0 and s["new_size"] < 0:
            s2 = dict(s); s2["event"] = "FLIP_TO_SHORT"; opens.append(s2)
        elif s["prev_size"] < 0 and s["delta"] < 0:
            s2 = dict(s); s2["event"] = "ADD_TO_SHORT"; opens.append(s2)
        elif s["prev_size"] > 0 and s["new_size"] < 0:
            s2 = dict(s); s2["event"] = "FLIP_LONG_TO_SHORT"; opens.append(s2)
    return opens


def replay_filters_at(coin: str, target_dt: datetime) -> dict:
    """Pull enough candle history up to target_dt and compute each 1h filter state.
    target_dt must be timezone-aware UTC."""
    # Fetch enough to build EMAs/RSI/HMA/SJM
    d5 = fetch_candles(coin, "5m", 1500)
    d1 = fetch_candles(coin, "1h", 500)
    d4 = fetch_candles(coin, "4h", 300)
    if d5.empty or d1.empty:
        return {"error": "empty candles"}

    # Filter to only bars closed at or before target_dt (avoid look-ahead)
    cutoff = pd.Timestamp(target_dt).tz_convert("UTC") if target_dt.tzinfo else pd.Timestamp(target_dt, tz="UTC")
    d5_trunc = d5[d5["timestamp"] <= cutoff].copy()
    d1_trunc = d1[d1["timestamp"] <= cutoff].copy()
    d4_trunc = d4[d4["timestamp"] <= cutoff].copy()
    if len(d5_trunc) < 60 or len(d1_trunc) < 20:
        return {"error": f"insufficient history at {target_dt}: 5m={len(d5_trunc)} 1h={len(d1_trunc)}"}

    d5f = add_features(d5_trunc)
    d1f = add_features(d1_trunc)
    d4f = add_features(d4_trunc) if not d4_trunc.empty else d4_trunc

    up_e, dn_e = trend_lookup_1h(d5f, d1f)
    up_s, dn_s = structure_lookup_1h(d5f, d1f)
    up_h, dn_h = hma_slope_lookup_1h(d5f, d1f)
    up_j, dn_j = sjm_lookup_1h(d5f, d1f)
    up_4h = dn_4h = None
    if not d4f.empty:
        u4, d4 = structure_lookup_1h(d5f, d4f, pivot_bars=3)
        up_4h, dn_4h = bool(u4[-1]), bool(d4[-1])

    def _last(arr, name):
        if arr is None or len(arr) == 0:
            return None
        return bool(arr[-1])

    return {
        "bar_ts": d5f["timestamp"].iloc[-1].isoformat(),
        "close": float(d5f["close"].iloc[-1]),
        "rsi": float(d5f["rsi"].iloc[-1]),
        "atr": float(d5f["atr"].iloc[-1]),
        "ema_cross_1h": (_last(up_e, "ue"), _last(dn_e, "de")),
        "structure_1h": (_last(up_s, "us"), _last(dn_s, "ds")),
        "hma_slope_1h": (_last(up_h, "uh"), _last(dn_h, "dh")),
        "sjm_1h":       (_last(up_j, "uj"), _last(dn_j, "dj")),
        "structure_4h": (up_4h, dn_4h),
    }


def main():
    # 58bro's BTC short entry is average $72,369, ETH $2,465
    # Pull 60 days of fills to find the opens
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 60 * 24 * 3600 * 1000
    print(f"Fetching 58bro fills from last 60 days...")
    fills = fetch_fills_by_time(WHALE_58BRO, start_ms, now_ms)
    print(f"Got {len(fills)} fills total.")

    for coin in ["BTC", "ETH"]:
        print(f"\n{'='*70}\n  {coin} position reconstruction\n{'='*70}")
        states = reconstruct_position_history(fills, coin)
        print(f"Total {coin} fills: {len(states)}")
        if not states:
            print(f"  (no {coin} fills in 60d window — position opened >60d ago)")
            continue

        # Find notable events: position flips, big adds
        opens = find_short_opens(states)
        if not opens:
            print(f"  No short opens in {coin} fills — already was short throughout window")
            print(f"  First fill: {states[0]['dt']} price ${states[0]['price']:.2f} size {states[0]['new_size']:+.2f}")
            print(f"  Last fill:  {states[-1]['dt']} price ${states[-1]['price']:.2f} size {states[-1]['new_size']:+.2f}")
            continue

        print(f"\nShort-open events on {coin} ({len(opens)}):")
        for ev in opens[-10:]:
            print(f"  {ev['dt'].strftime('%Y-%m-%d %H:%M UTC')} | {ev['event']:<20} "
                  f"Δ={ev['delta']:+.2f}  px=${ev['price']:.2f}  new_sz={ev['new_size']:+.2f}")

        # Replay filters at the very first short open we found
        first_short = opens[0]
        print(f"\n>> Replaying our filters as of first short-open on {coin}:")
        print(f"   {first_short['dt']} @ ${first_short['price']:.2f}")
        result = replay_filters_at(coin, first_short["dt"])
        if "error" in result:
            print(f"   ERROR: {result['error']}")
        else:
            print(f"   Our 5m bar at that time: close=${result['close']:.2f} rsi={result['rsi']:.1f}")
            print(f"   Filter reads (U=up, D=dn):")
            for k in ("ema_cross_1h", "structure_1h", "hma_slope_1h", "sjm_1h", "structure_4h"):
                u, d = result[k]
                u_s = "U" if u else ("-" if u is False else "?")
                d_s = "D" if d else ("-" if d is False else "?")
                print(f"     {k:<18} {u_s}{d_s}")
            # Direction the whale took
            print(f"   58bro direction: SHORT → needs dn=True on all enforced filters")


if __name__ == "__main__":
    main()
