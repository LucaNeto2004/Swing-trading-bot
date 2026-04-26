"""For each 58bro short-open episode, pull price action AFTER the entry and
measure whether our 'UP' filter call was actually correct — i.e. did price
go up (we're right, 58bro lost) or down (we're wrong, 58bro won) at various
forward windows (1h / 4h / 24h / 3d / current)."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

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
            "close": float(c["c"])} for c in raw])
        return df.sort_values("timestamp").reset_index(drop=True)
    return pd.DataFrame()


def find_entry_episodes(fills: list[dict], coin: str) -> list[dict]:
    sym = [f for f in fills if f.get("coin") == coin]
    sym.sort(key=lambda x: int(x["time"]))
    eps = []
    cur = None
    for f in sym:
        if "Open Short" not in f.get("dir", ""):
            if cur is not None:
                eps.append(cur); cur = None
            continue
        ts = int(f["time"])
        sz = float(f["sz"])
        if cur is None or (ts - cur["last_ts"]) / 60000 > 15:
            if cur is not None: eps.append(cur)
            cur = {"first_ts": ts, "last_ts": ts, "size": 0.0, "px_x_sz": 0.0}
        cur["last_ts"] = ts
        cur["size"] += sz
        cur["px_x_sz"] += float(f["px"]) * sz
    if cur is not None: eps.append(cur)
    for ep in eps:
        ep["vwap"] = ep["px_x_sz"] / max(ep["size"], 1e-9)
        ep["dt"] = datetime.fromtimestamp(ep["first_ts"] / 1000, tz=timezone.utc)
    return eps


def forward_returns_at(coin: str, entry_ms: int, entry_px: float) -> dict:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = fetch_candles_range(coin, "1h", entry_ms - 3_600_000, now_ms)
    if df.empty:
        return {"error": "no candles"}
    entry_ts = pd.Timestamp(entry_ms, unit="ms", tz="UTC")
    horizons_h = {"1h": 1, "4h": 4, "24h": 24, "72h": 72, "current": None}
    out = {}
    for label, h in horizons_h.items():
        if h is None:
            row = df.iloc[-1]
        else:
            target = entry_ts + pd.Timedelta(hours=h)
            sub = df[df["timestamp"] >= target]
            if sub.empty:
                out[label] = None; continue
            row = sub.iloc[0]
        px = float(row["close"])
        pct = (px / entry_px - 1) * 100
        out[label] = {"px": px, "pct": pct}
    return out


def verdict(px_move_pct: float) -> str:
    """For a SHORT entry — UP (positive move) means our 'up' call was RIGHT
    (we blocked a losing short). DOWN (negative move) means we were WRONG
    (we blocked a winning short)."""
    if px_move_pct is None: return "?"
    if px_move_pct > 0.5:  return f"UP {px_move_pct:+.2f}% → we RIGHT"
    if px_move_pct < -0.5: return f"DN {px_move_pct:+.2f}% → we WRONG"
    return f"flat {px_move_pct:+.2f}%"


def analyze(coin: str, fills: list[dict]):
    eps = find_entry_episodes(fills, coin)
    print(f"\n{'='*78}\n  58bro {coin} — forward returns after each short-open\n{'='*78}")
    correct_counts = {"1h": [0, 0, 0], "4h": [0, 0, 0], "24h": [0, 0, 0],
                      "72h": [0, 0, 0], "current": [0, 0, 0]}  # [right, wrong, flat]
    for i, ep in enumerate(eps):
        print(f"\n  Ep {i+1}: {ep['dt'].strftime('%Y-%m-%d %H:%M UTC')}  "
              f"VWAP=${ep['vwap']:,.2f}  size={ep['size']:.1f}")
        fr = forward_returns_at(coin, ep["first_ts"], ep["vwap"])
        if "error" in fr:
            print(f"    err: {fr['error']}"); continue
        for h, result in fr.items():
            if result is None:
                print(f"    {h:<8} (not enough forward data)"); continue
            verdict_str = verdict(result["pct"])
            print(f"    {h:<8} px=${result['px']:,.2f}  {verdict_str}")
            if result["pct"] > 0.5: correct_counts[h][0] += 1
            elif result["pct"] < -0.5: correct_counts[h][1] += 1
            else: correct_counts[h][2] += 1

    print(f"\n  SCORECARD on {coin} ({len(eps)} entries):")
    print(f"  {'horizon':<10} {'our UP right':<14} {'our UP wrong':<14} flat")
    for h in ("1h", "4h", "24h", "72h", "current"):
        r, w, f = correct_counts[h]
        print(f"  {h:<10} {r:<14} {w:<14} {f}")


def main():
    print("Fetching 58bro fills...")
    fills = fetch_all_fills(WHALE_58BRO)
    print(f"{len(fills)} fills fetched.")
    analyze("BTC", fills)
    analyze("ETH", fills)


if __name__ == "__main__":
    main()
