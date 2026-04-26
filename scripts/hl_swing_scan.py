"""HL swing-trader scanner.

Given a list of candidate wallet addresses, pull userFills from the HL API,
roll fills into round-trip trades per coin, and score each wallet on
swing-style metrics: avg hold time, win rate, coin mix, 30d realized PnL.

Usage:
    python scripts/hl_swing_scan.py                 # scans default seed list
    python scripts/hl_swing_scan.py 0xabc... 0xdef... # scan specific wallets

A wallet is flagged SWING-STYLE when:
    avg_hold_hours >= 4  AND  coins_traded >= 3  AND  win_rate_30d >= 0.55
    AND  realized_pnl_30d > 0
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Union

HL_API = "https://api.hyperliquid.xyz/info"

# Seed list: publicly-reported HL traders, plus room to extend.
# These are starting points — not pre-vetted as swing traders. The scanner decides.
SEED_WALLETS = [
    "0x0Ddf9bAe2aF4B874B96D287a5aD42Eb47138A902",  # pension-usdt.eth
    "0xfa6af5f4f7440ce389a1e650991eea45c161e13e",  # TheWhiteWhaleHL w1
    "0xa04a4b7b7c37dbd271fdc57618e9cb9836b250bf",  # TheWhiteWhaleHL w2
    "0xb8b9e3097c8b1dddf9c5ea9d48a7ebeaf09d67d2",  # TheWhiteWhaleHL w3
    "0xd5ff5491f6f3c80438e02c281726757baf4d1070",  # TheWhiteWhaleHL w4
    "0x5078c2fbea2b2ad61bc840bc023e35fce56bedb6",  # Coinglass whale sample
]

MS_30D = 30 * 24 * 3600 * 1000


def _post(body: dict) -> Union[list, dict]:
    req = urllib.request.Request(
        HL_API,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                time.sleep(0.3)
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("max retries")


def fetch_state(addr: str) -> dict:
    return _post({"type": "clearinghouseState", "user": addr})


def fetch_fills(addr: str, lookback_days: int = 30) -> list:
    """Paginated fetch via userFillsByTime. Walks startTime forward until we
    catch up to now. Caps at 20 pages (~40k fills) to avoid runaway on whales
    that do thousands of TWAP slices — those aren't swing traders anyway."""
    now_ms = int(time.time() * 1000)
    start = now_ms - lookback_days * 24 * 3600 * 1000
    all_fills: list = []
    for _ in range(20):
        batch = _post({
            "type": "userFillsByTime",
            "user": addr,
            "startTime": start,
            "endTime": now_ms,
        })
        if not isinstance(batch, list) or not batch:
            break
        all_fills.extend(batch)
        if len(batch) < 2000:
            break
        newest_in_batch = max(f["time"] for f in batch)
        if newest_in_batch <= start:
            break
        start = newest_in_batch + 1
    seen = set()
    uniq = []
    for f in all_fills:
        key = (f["tid"], f["time"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq


@dataclass
class Trade:
    coin: str
    open_ts: int
    close_ts: int
    pnl: float
    direction: str  # "long" | "short"


def _roll_trades(fills: list):
    """Collapse fills into round-trip trades per coin via startPosition flips.

    A round-trip is the span from when |position| goes from ~0 to non-zero until
    it returns to ~0. closedPnl sums inside the span give trade PnL.
    """
    by_coin: dict[str, list] = defaultdict(list)
    for f in fills:
        by_coin[f["coin"]].append(f)

    trades = []
    for coin, seq in by_coin.items():
        seq.sort(key=lambda f: f["time"])
        open_ts = None
        pnl_accum = 0.0
        direction = None
        for f in seq:
            start_pos = float(f["startPosition"])
            sz = float(f["sz"])
            side = f["side"]  # "A" = sell/ask, "B" = buy/bid
            signed = sz if side == "B" else -sz
            end_pos = start_pos + signed
            pnl_accum += float(f["closedPnl"])

            if open_ts is None and abs(start_pos) < 1e-9 and abs(end_pos) > 1e-9:
                open_ts = f["time"]
                direction = "long" if end_pos > 0 else "short"
                pnl_accum = float(f["closedPnl"])  # reset at open
            elif open_ts is not None and abs(end_pos) < 1e-9:
                trades.append(Trade(
                    coin=coin,
                    open_ts=open_ts,
                    close_ts=f["time"],
                    pnl=pnl_accum,
                    direction=direction or "unknown",
                ))
                open_ts = None
                pnl_accum = 0.0
                direction = None
    return trades


def score(addr: str) -> dict:
    try:
        state = fetch_state(addr)
        fills = fetch_fills(addr, lookback_days=45)
    except Exception as e:
        return {"addr": addr, "error": str(e)}

    av = float(state.get("marginSummary", {}).get("accountValue", 0))
    ntl = float(state.get("marginSummary", {}).get("totalNtlPos", 0))
    open_positions = [
        f"{p['position']['coin']}:{p['position']['szi']}"
        for p in state.get("assetPositions", [])
    ]

    if not fills:
        empty = {"n": 0, "pnl": 0.0, "wr": 0.0, "avg_hold_h": 0.0, "median_hold_h": 0.0, "coins": 0}
        return {
            "addr": addr, "account_value": av, "open_ntl": ntl,
            "open_positions": open_positions,
            "all_time": empty, "last_30d": empty, "flag": "no_fills",
        }

    trades = _roll_trades(fills)
    now_ms = int(time.time() * 1000)
    recent = [t for t in trades if t.close_ts >= now_ms - MS_30D]

    def _metrics(ts) -> dict:
        if not ts:
            return {"n": 0, "pnl": 0.0, "wr": 0.0, "avg_hold_h": 0.0, "coins": 0}
        pnl = sum(t.pnl for t in ts)
        wins = sum(1 for t in ts if t.pnl > 0)
        hold_hours = [(t.close_ts - t.open_ts) / 3_600_000 for t in ts]
        coins = len({t.coin for t in ts})
        return {
            "n": len(ts),
            "pnl": pnl,
            "wr": wins / len(ts),
            "avg_hold_h": sum(hold_hours) / len(hold_hours),
            "median_hold_h": sorted(hold_hours)[len(hold_hours) // 2],
            "coins": coins,
        }

    all_m = _metrics(trades)
    m30 = _metrics(recent)

    swing = (
        m30["n"] >= 3
        and m30["avg_hold_h"] >= 4
        and m30["coins"] >= 2
        and m30["wr"] >= 0.55
        and m30["pnl"] > 0
    )

    return {
        "addr": addr,
        "account_value": av,
        "open_ntl": ntl,
        "open_positions": open_positions,
        "all_time": all_m,
        "last_30d": m30,
        "flag": "SWING" if swing else "other",
    }


def main():
    wallets = sys.argv[1:] if len(sys.argv) > 1 else SEED_WALLETS
    print(f"Scanning {len(wallets)} wallets…\n")

    rows = []
    for addr in wallets:
        r = score(addr)
        rows.append(r)
        if "error" in r:
            print(f"{addr[:10]}…{addr[-4:]}  ERROR: {r['error']}")
            continue
        m30 = r["last_30d"]
        print(
            f"{addr[:10]}…{addr[-4:]}  AV=${r['account_value']:>12,.0f}  "
            f"30d: n={m30['n']:>3} pnl=${m30['pnl']:>+12,.0f} "
            f"wr={m30['wr']*100:>4.0f}% hold={m30['avg_hold_h']:>5.1f}h "
            f"coins={m30['coins']:>2}  [{r['flag']}]"
        )

    print()
    swing = [r for r in rows if r.get("flag") == "SWING"]
    if swing:
        print(f"Swing candidates ({len(swing)}):")
        for r in swing:
            print(f"  {r['addr']}  30d pnl=${r['last_30d']['pnl']:+,.0f}")
    else:
        print("No wallets matched SWING criteria in this seed list.")
        print("Add more addresses: python scripts/hl_swing_scan.py 0xabc… 0xdef…")

    out = "/tmp/hl_swing_scan.json"
    with open(out, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
