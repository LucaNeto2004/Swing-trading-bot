"""Discord webhook alerts — async, non-blocking."""
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from config.settings import BotConfig
from utils.logger import setup_logger

log = setup_logger("alerts")
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


class AlertManager:
    def __init__(self, config: BotConfig):
        self.config = config
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert")
        self._pending: list = []
        self._lock = threading.Lock()

    def _fire(self, url: str, payload: dict):
        if not url:
            return
        fut = self._pool.submit(self._do_send, url, payload)
        with self._lock:
            self._pending.append(fut)
            self._pending = [f for f in self._pending if not f.done()]

    @staticmethod
    def _do_send(url: str, payload: dict):
        for attempt in range(3):
            try:
                resp = _session.post(url, json=payload, timeout=5)
                if resp.status_code not in (200, 204):
                    log.warning(f"Discord returned {resp.status_code}: {resp.text[:200]}")
                return
            except Exception as e:
                if attempt < 2:
                    continue
                log.error(f"Discord alert failed after 3 retries: {e}")

    def send_entry(self, symbol: str, side: str, price: float, reason: str,
                   size: float, notional: float, sl: float, tp1: Optional[float]):
        color = 0x2ECC71 if side == "long" else 0xE74C3C
        fields = [
            {"name": "Side", "value": side.upper(), "inline": True},
            {"name": "Price", "value": f"${price:.4f}", "inline": True},
            {"name": "Size", "value": f"{size:.4f}", "inline": True},
            {"name": "Notional", "value": f"${notional:,.2f}", "inline": True},
            {"name": "SL", "value": f"${sl:.4f}", "inline": True},
        ]
        if tp1 is not None:
            fields.append({"name": "TP1", "value": f"${tp1:.4f}", "inline": True})
        self._fire(self.config.discord_webhook_trades, {
            "embeds": [{
                "title": f"🐋 ENTRY {symbol}",
                "description": reason,
                "color": color,
                "fields": fields,
            }]
        })

    def send_exit(self, symbol: str, side: str, price: float, pnl: float, reason: str,
                  held_bars: int):
        color = 0x2ECC71 if pnl >= 0 else 0xE74C3C
        self._fire(self.config.discord_webhook_trades, {
            "embeds": [{
                "title": f"🐋 EXIT {symbol} — {reason}",
                "description": f"{side.upper()} closed @ ${price:.4f} | P&L: ${pnl:+.2f} | held {held_bars} bars",
                "color": color,
            }]
        })

    def send_status(self, level: str, message: str):
        colors = {"online": 0x2ECC71, "offline": 0x95A5A6, "warning": 0xF39C12, "error": 0xE74C3C}
        self._fire(self.config.discord_webhook_alerts, {
            "embeds": [{
                "title": f"Bot {level.upper()}",
                "description": message,
                "color": colors.get(level, 0x3498DB),
            }]
        })

    def flush(self, timeout: float = 5.0):
        with self._lock:
            pending = list(self._pending)
        for fut in pending:
            try:
                fut.result(timeout=timeout)
            except Exception:
                pass
