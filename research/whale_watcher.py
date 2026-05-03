"""58bro wallet watcher — shadow-only position monitor.

Polls 58bro's HL account via `shared.hl_client`, diffs against the last
snapshot in ``data/whale_snapshots/<addr>.json``, and fires Discord webhooks
on material changes (new position / closed position / pyramided size).

This is read-only and never places orders. Run standalone:

    python -m research.whale_watcher          # one poll, print + alert
    python -m research.whale_watcher --loop   # poll every POLL_SECS
"""
from __future__ import annotations

import argparse
import json
import os
import sys as _sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_BASE_DIR, ".env"))
_SHARED_DIR = os.path.join(_BASE_DIR, "shared")
if not os.path.isdir(_SHARED_DIR):
    _SHARED_DIR = os.path.abspath(os.path.join(_BASE_DIR, "..", "shared"))
if _SHARED_DIR not in _sys.path:
    _sys.path.insert(0, _SHARED_DIR)
if _BASE_DIR not in _sys.path:
    _sys.path.insert(0, _BASE_DIR)

import hl_client  # noqa: E402 — from shared/
from utils.logger import setup_logger  # noqa: E402

log = setup_logger("whale_watcher")

WHALE_58BRO = "0x418AA6Bf98a2b2BC93779f810330d88cDe488888"           # 58bro main ($7M)
WHALE_NERVOUSDEGEN = "0xa4deddA59F2908b92AE192cfD494839373bCB3C4"    # nervousdegen — different trader, LIT long
WHALE_WATCHLIST = [
    ("58bro", WHALE_58BRO),
    ("nervousdegen", WHALE_NERVOUSDEGEN),
]
SNAPSHOT_DIR = os.path.join(_BASE_DIR, "data", "whale_snapshots")
POLL_SECS = 300  # 5 min
SIZE_CHANGE_THRESHOLD = 0.05  # alert on >=5% size change (pyramid / partial)


@dataclass
class Snapshot:
    timestamp: str
    account_value: float
    positions: dict  # coin -> { size, side, entry, unrl, liq }

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "account_value": self.account_value,
                "positions": self.positions}


def _load_last(addr: str) -> Optional[Snapshot]:
    path = os.path.join(SNAPSHOT_DIR, f"{addr.lower()}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
        return Snapshot(
            timestamp=d.get("timestamp", ""),
            account_value=float(d.get("account_value", 0.0)),
            positions=d.get("positions", {}),
        )
    except Exception as e:
        log.warning(f"failed to load snapshot for {addr}: {e}")
        return None


def _save(addr: str, snap: Snapshot) -> None:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{addr.lower()}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap.to_dict(), f, indent=2, default=str)
    os.replace(tmp, path)


def _fetch(addr: str) -> Snapshot:
    balance = hl_client.sync_get_balance(addr)
    positions = hl_client.sync_get_positions(addr)
    pos_map: dict = {}
    for p in positions:
        pos_map[p["coin"]] = {
            "size": p["size"],
            "side": p["side"],
            "entry": p["entry_price"],
            "unrl": p["unrealised_pnl"],
            "liq": p["liquidation_price"],
            "leverage": p.get("leverage", {}),
        }
    return Snapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        account_value=balance["account_value"],
        positions=pos_map,
    )


def _discord_send(webhook: str, embed: dict) -> None:
    if not webhook:
        log.warning("no Discord webhook configured; skipping alert")
        return
    try:
        r = requests.post(webhook, json={"embeds": [embed]}, timeout=5)
        if r.status_code not in (200, 204):
            log.warning(f"Discord returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Discord alert failed: {e}")


def _fmt_notional(size: float, entry: float) -> str:
    return f"${abs(size * entry):,.0f}"


def _diff_and_alert(addr: str, prev: Optional[Snapshot], curr: Snapshot, webhook: str) -> int:
    """Compare two snapshots; fire one Discord embed per material change.
    Returns number of alerts sent."""
    sent = 0
    prev_pos = prev.positions if prev else {}
    curr_pos = curr.positions

    new_coins = set(curr_pos) - set(prev_pos)
    closed_coins = set(prev_pos) - set(curr_pos)
    kept_coins = set(curr_pos) & set(prev_pos)

    short_addr = f"{addr[:6]}…{addr[-4:]}"

    for coin in sorted(new_coins):
        p = curr_pos[coin]
        color = 0x2ECC71 if p["side"] == "long" else 0xE74C3C
        _discord_send(webhook, {
            "title": f"🐋 58bro OPENED {p['side'].upper()} {coin}",
            "description": f"wallet {short_addr}",
            "color": color,
            "fields": [
                {"name": "Size", "value": f"{p['size']:+,.2f}", "inline": True},
                {"name": "Entry", "value": f"${p['entry']:.4f}", "inline": True},
                {"name": "Notional", "value": _fmt_notional(p["size"], p["entry"]), "inline": True},
                {"name": "Liq", "value": f"${p['liq']:.4f}", "inline": True},
                {"name": "Unrl P&L", "value": f"${p['unrl']:+,.0f}", "inline": True},
            ],
        })
        sent += 1

    for coin in sorted(closed_coins):
        p = prev_pos[coin]
        _discord_send(webhook, {
            "title": f"🐋 58bro CLOSED {p['side'].upper()} {coin}",
            "description": f"wallet {short_addr}",
            "color": 0x95A5A6,
            "fields": [
                {"name": "Was size", "value": f"{p['size']:+,.2f}", "inline": True},
                {"name": "Was entry", "value": f"${p['entry']:.4f}", "inline": True},
            ],
        })
        sent += 1

    for coin in sorted(kept_coins):
        prev_sz = prev_pos[coin]["size"]
        curr_sz = curr_pos[coin]["size"]
        if prev_sz == 0:
            continue
        rel = (curr_sz - prev_sz) / abs(prev_sz)
        if abs(rel) < SIZE_CHANGE_THRESHOLD:
            continue
        direction = "PYRAMID +" if rel > 0 else "TRIMMED "
        p = curr_pos[coin]
        color = 0x3498DB if rel > 0 else 0xF39C12
        _discord_send(webhook, {
            "title": f"🐋 58bro {direction}{coin} {abs(rel)*100:.0f}%",
            "description": f"wallet {short_addr}",
            "color": color,
            "fields": [
                {"name": "New size", "value": f"{curr_sz:+,.2f}", "inline": True},
                {"name": "Prev size", "value": f"{prev_sz:+,.2f}", "inline": True},
                {"name": "Entry (avg)", "value": f"${p['entry']:.4f}", "inline": True},
                {"name": "Notional", "value": _fmt_notional(curr_sz, p["entry"]), "inline": True},
            ],
        })
        sent += 1

    if prev is not None:
        acct_delta = curr.account_value - prev.account_value
        if abs(acct_delta) > 100_000 and prev.account_value > 0:
            _discord_send(webhook, {
                "title": f"🐋 58bro account value change",
                "description": f"wallet {short_addr}",
                "color": 0x2ECC71 if acct_delta >= 0 else 0xE74C3C,
                "fields": [
                    {"name": "Now", "value": f"${curr.account_value:,.0f}", "inline": True},
                    {"name": "Was", "value": f"${prev.account_value:,.0f}", "inline": True},
                    {"name": "Δ", "value": f"${acct_delta:+,.0f}", "inline": True},
                ],
            })
            sent += 1

    return sent


def poll_once(addr: str = WHALE_58BRO, webhook: str | None = None) -> None:
    webhook = webhook or os.environ.get("DISCORD_WEBHOOK_WHALE") or os.environ.get("DISCORD_WEBHOOK_ALERTS", "")
    prev = _load_last(addr)
    curr = _fetch(addr)
    sent = _diff_and_alert(addr, prev, curr, webhook)
    _save(addr, curr)
    if prev is None:
        log.info(f"{addr[:10]}: first snapshot written, {len(curr.positions)} positions, "
                 f"account ${curr.account_value:,.0f}")
    else:
        log.info(f"{addr[:10]}: {len(curr.positions)} positions, account ${curr.account_value:,.0f}, "
                 f"{sent} alerts fired")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=WHALE_58BRO)
    ap.add_argument("--loop", action="store_true", help=f"poll every {POLL_SECS}s")
    ap.add_argument("--webhook", default=None, help="Discord webhook URL (overrides env)")
    args = ap.parse_args()

    # If user passed a specific --addr, watch only that one; otherwise watch the full watchlist
    targets = [("custom", args.addr)] if args.addr != WHALE_58BRO else WHALE_WATCHLIST
    if args.loop:
        while True:
            for name, addr in targets:
                try:
                    poll_once(addr, args.webhook)
                except Exception as e:
                    log.error(f"{name} poll failed: {e}", exc_info=True)
            time.sleep(POLL_SECS)
    else:
        for name, addr in targets:
            try:
                poll_once(addr, args.webhook)
            except Exception as e:
                log.error(f"{name} poll failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
