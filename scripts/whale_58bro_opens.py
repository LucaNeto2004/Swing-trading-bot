"""Find 58bro's BTC + ETH SHORT OPEN fills (not closes) in the last ~25d of
fills, cluster them into 'entry episodes', then replay our bot filters at each
entry and show whether we would have taken the same trade."""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import httpx
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from core.features import add_features, trend_lookup_1h, structure_lookup_1h, hma_slope_lookup_1h, sjm_lookup_1h

# Historical candle fetcher — explicit start/end instead of bars-from-now
def fetch_candles_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    payload = {"type": "candleSnapshot",
               "req": {"coin": symbol, "interval": interval,
                       "startTime": start_ms, "endTime": end_ms}}
    for attempt in range(5):
        r = httpx.post(HL_INFO_URL, json=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(2 ** attempt); continue
        r.raise_for_status()
        raw = r.json() or []
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "timestamp": pd.to_datetime(int(c["t"]), unit="ms", utc=True),
            "open": float(c["o"]), "high": float(c["h"]),
            "low": float(c["l"]), "close": float(c["c"]),
            "volume": float(c["v"])} for c in raw])
        return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return pd.DataFrame()

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
WHALE_58BRO = "0x418AA6Bf98a2b2BC93779f810330d88cDe488888"


def fetch_all_fills(addr: str) -> list[dict]:
    for attempt in range(5):
        r = httpx.post(HL_INFO_URL, json={"type": "userFills", "user": addr}, timeout=20)
        if r.status_code == 429:
            time.sleep(2 ** attempt); continue
        r.raise_for_status()
        return r.json() or []
    raise RuntimeError("rate limit")


def classify_fills(fills: list[dict], coin: str) -> list[dict]:
    """Return sorted (oldest-first) list of parsed fills for one coin."""
    out = []
    for f in fills:
        if f.get("coin") != coin:
            continue
        sz = float(f["sz"])
        signed = sz if f["side"] == "B" else -sz
        out.append({
            "ts_ms": int(f["time"]),
            "dt": datetime.fromtimestamp(int(f["time"]) / 1000, tz=timezone.utc),
            "dir": f.get("dir", ""),
            "delta": signed,
            "price": float(f["px"]),
            "start_position": float(f.get("startPosition", 0) or 0),
            "closed_pnl": float(f.get("closedPnl", 0)),
            "fee": float(f.get("fee", 0)),
        })
    out.sort(key=lambda x: x["ts_ms"])
    return out


def find_open_short_episodes(sym_fills: list[dict]) -> list[dict]:
    """Group consecutive 'Open Short' fills into single 'entry episodes'. One
    whale intent can produce dozens of small fills within a minute."""
    episodes = []
    cur = None
    GAP_MIN = 15  # minutes gap → new episode
    for f in sym_fills:
        is_open_short = "Open Short" in f["dir"] or (f["dir"] == "" and f["delta"] < 0)
        if not is_open_short:
            if cur is not None:
                episodes.append(cur); cur = None
            continue
        if cur is None or (f["ts_ms"] - cur["last_ts_ms"]) / 60000 > GAP_MIN:
            if cur is not None:
                episodes.append(cur)
            cur = {
                "first_ts_ms": f["ts_ms"],
                "last_ts_ms": f["ts_ms"],
                "first_dt": f["dt"],
                "total_size": 0.0,
                "vwap_numer": 0.0,
                "fills": 0,
            }
        cur["last_ts_ms"] = f["ts_ms"]
        cur["total_size"] += f["delta"]  # negative (short)
        cur["vwap_numer"] += abs(f["delta"]) * f["price"]
        cur["fills"] += 1
    if cur is not None:
        episodes.append(cur)
    for ep in episodes:
        ep["vwap"] = ep["vwap_numer"] / max(abs(ep["total_size"]), 1e-9)
        ep["duration_s"] = (ep["last_ts_ms"] - ep["first_ts_ms"]) / 1000
    return episodes


def replay_filters_at(coin: str, target_dt: datetime) -> dict:
    cutoff_ms = int(target_dt.timestamp() * 1000)
    # Pull enough history up to the target time
    d5 = fetch_candles_range(coin, "5m", cutoff_ms - 1500 * 300_000, cutoff_ms)
    d1 = fetch_candles_range(coin, "1h", cutoff_ms - 500 * 3_600_000, cutoff_ms)
    d4 = fetch_candles_range(coin, "4h", cutoff_ms - 300 * 14_400_000, cutoff_ms)
    if d5.empty or d1.empty:
        return {"error": "empty candles"}
    cutoff = pd.Timestamp(target_dt).tz_convert("UTC") if target_dt.tzinfo else pd.Timestamp(target_dt, tz="UTC")
    d5 = d5[d5["timestamp"] <= cutoff].copy()
    d1 = d1[d1["timestamp"] <= cutoff].copy()
    d4 = d4[d4["timestamp"] <= cutoff].copy()
    if len(d5) < 60 or len(d1) < 20:
        return {"error": f"5m={len(d5)} 1h={len(d1)} insufficient for cutoff {cutoff}"}
    d5f = add_features(d5); d1f = add_features(d1)
    d4f = add_features(d4) if not d4.empty else d4

    up_e, dn_e = trend_lookup_1h(d5f, d1f)
    up_s, dn_s = structure_lookup_1h(d5f, d1f)
    up_h, dn_h = hma_slope_lookup_1h(d5f, d1f)
    up_j, dn_j = sjm_lookup_1h(d5f, d1f)
    up_4h = dn_4h = None
    if not d4f.empty:
        u4, d4x = structure_lookup_1h(d5f, d4f, pivot_bars=3)
        up_4h = bool(u4[-1]); dn_4h = bool(d4x[-1])

    def _last(arr):
        return bool(arr[-1]) if arr is not None and len(arr) else None

    return {
        "bar_ts": d5f["timestamp"].iloc[-1].isoformat(),
        "close": float(d5f["close"].iloc[-1]),
        "rsi": float(d5f["rsi"].iloc[-1]),
        "atr": float(d5f["atr"].iloc[-1]),
        "ema_cross_1h":  (_last(up_e), _last(dn_e)),
        "structure_1h":  (_last(up_s), _last(dn_s)),
        "hma_slope_1h":  (_last(up_h), _last(dn_h)),
        "sjm_1h":        (_last(up_j), _last(dn_j)),
        "structure_4h":  (up_4h, dn_4h),
    }


def fmt_ud(pair):
    u, d = pair
    if u is None and d is None: return "? ?"
    return ("U" if u else "-") + ("D" if d else "-")


def analyze_coin(coin: str, fills: list[dict]):
    print(f"\n{'='*78}\n  58bro {coin} — SHORT OPEN episode analysis\n{'='*78}")
    sym = classify_fills(fills, coin)
    if not sym:
        print(f"  no {coin} fills")
        return
    # How many opens vs closes
    opens = [f for f in sym if "Open Short" in f["dir"]]
    closes = [f for f in sym if "Close Short" in f["dir"]]
    other = [f for f in sym if "Open Short" not in f["dir"] and "Close Short" not in f["dir"]]
    print(f"  total {coin} fills: {len(sym)}  |  Open Short: {len(opens)}  Close Short: {len(closes)}  Other: {len(other)}")
    if not opens:
        print(f"  No Open-Short fills in window — position opened before {sym[0]['dt']}")
        return
    episodes = find_open_short_episodes(sym)
    print(f"  Entry episodes (grouped by <15min gap): {len(episodes)}")

    for i, ep in enumerate(episodes[:20]):
        total_notional = abs(ep["total_size"]) * ep["vwap"]
        print(f"\n  Episode {i+1}: {ep['first_dt'].strftime('%Y-%m-%d %H:%M UTC')}  "
              f"({ep['fills']} fills over {ep['duration_s']:.0f}s)")
        print(f"    VWAP=${ep['vwap']:,.2f}  size={ep['total_size']:+.2f}  notional≈${total_notional:,.0f}")
        # Replay filters at start of episode
        res = replay_filters_at(coin, ep["first_dt"])
        if "error" in res:
            print(f"    filter replay ERROR: {res['error']}")
            continue
        print(f"    our 5m bar at entry: close=${res['close']:.2f}  rsi={res['rsi']:.1f}")
        print(f"    filter reads:   ema_1h={fmt_ud(res['ema_cross_1h'])}   "
              f"str_1h={fmt_ud(res['structure_1h'])}   "
              f"hma_1h={fmt_ud(res['hma_slope_1h'])}   "
              f"sjm_1h={fmt_ud(res['sjm_1h'])}   "
              f"str_4h={fmt_ud(res['structure_4h'])}")
        # For SHORT, we want dn=True on filters
        shorts_allowed = {
            "both_agree": (res["ema_cross_1h"][1] and res["structure_1h"][1]),
            "hma_slope":  res["hma_slope_1h"][1],
            "sjm":        res["sjm_1h"][1],
        }
        blocked_4h = res["structure_4h"][0] and not res["structure_4h"][1]
        print(f"    SHORT-allowed under each filter variant: {shorts_allowed}")
        if blocked_4h:
            print(f"    *** 4h structure = UP → our 4h gate would have BLOCKED this entry ***")


def main():
    print("Fetching 58bro fills...")
    fills = fetch_all_fills(WHALE_58BRO)
    print(f"Got {len(fills)} total fills")
    analyze_coin("BTC", fills)
    analyze_coin("ETH", fills)


if __name__ == "__main__":
    main()
