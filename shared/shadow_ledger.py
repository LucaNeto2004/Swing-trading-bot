"""Virtual trade ledger for HL-native shadow execution.

Tracks what the local Python strategy's signals WOULD have produced if
executed with the bot's standard exit logic (SL/trail). Does NOT touch
paper_state.json, risk_state.json, or the bot's real balance. Purely an
observational ledger to validate "Python-on-HL" expected P&L against
the real TV-webhook-driven P&L before flipping the HL-native switch.

Used by both commodities-bot and crypto-bot:
  from shared.shadow_ledger import ShadowLedger

The ledger files live at:
  commodities-bot/data/hl_native_shadow_trades.json
  crypto-bot/data/hl_native_shadow_trades.json

Commission: 0.006% × 2 (HL blended maker/taker, matches research/backtester.py).
Position sizing: fixed notional per entry (default $200, configurable).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


COMMISSION_RATE = 0.00006  # per side


class ShadowLedger:
    def __init__(
        self,
        path: str,
        starting_balance: float = 10_000.0,
        position_pct: float = 0.20,
        size_usd: Optional[float] = None,
    ):
        """Shadow trade ledger that mirrors the real bot's position sizing.

        By default (position_pct=0.20, size_usd=None), each virtual entry is
        sized as `current_balance * position_pct` — exactly what
        core/execution.py does in the real bot. This makes shadow P&L
        directly comparable to live paper P&L without a scaling factor.

        If `size_usd` is passed explicitly (legacy mode), it overrides the
        balance-based sizing and uses a fixed notional per entry. Used only
        for tests and retroactive migration.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.starting_balance = starting_balance
        self.position_pct = position_pct
        self.size_usd = size_usd  # None = dynamic (balance × pct), else fixed
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text())
                # Tolerate missing keys by filling defaults
                state.setdefault("open_positions", {})
                state.setdefault("closed_trades", [])
                state.setdefault("balance", self.starting_balance)
                state.setdefault("starting_balance", self.starting_balance)
                return state
            except Exception:
                pass
        return {
            "open_positions": {},  # vid -> virtual position dict
            "closed_trades": [],    # list of closed trade records
            "balance": self.starting_balance,
            "starting_balance": self.starting_balance,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save(self) -> None:
        # Atomic write with unique tmp to avoid race condition (two threads saving).
        tmp = self.path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(self._state, indent=2, default=str))
            os.replace(tmp, self.path)
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ---------- Public API ----------

    def open_virtual(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: Optional[float],
        trail_offset: Optional[float],
        strategy_name: str,
        reason: str,
    ) -> str:
        """Open a virtual position. Returns the virtual position id (vid).

        Position size mirrors the real bot: current_balance × position_pct.
        As the ledger's virtual balance grows/shrinks with P&L, future entry
        sizes compound accordingly — same as the real paper bot."""
        now = datetime.now(timezone.utc)
        vid = f"{symbol}_{side}_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"
        # Dynamic sizing: balance × position_pct (matches core/execution.py).
        # Legacy fixed-size path used only for tests and migration.
        if self.size_usd is not None:
            entry_size_usd = float(self.size_usd)
        else:
            entry_size_usd = float(self._state["balance"] * self.position_pct)
        size = entry_size_usd / entry_price
        self._state["open_positions"][vid] = {
            "vid": vid,
            "symbol": symbol,
            "side": side,
            "entry_price": float(entry_price),
            "entry_time": now.isoformat(),
            "size": size,
            "size_usd": entry_size_usd,
            "stop_loss": float(stop_loss) if stop_loss is not None else None,
            "trail_offset": float(trail_offset) if trail_offset else None,
            "trail_active": False,
            "best_price": float(entry_price),
            "strategy": strategy_name,
            "reason": reason,
        }
        self._save()
        return vid

    def update_prices(self, prices: dict) -> int:
        """Called periodically with {symbol: current_price}.
        Updates trails and closes positions on SL hit. Returns number closed."""
        if not self._state["open_positions"]:
            return 0
        to_close = []
        for vid, pos in list(self._state["open_positions"].items()):
            price = prices.get(pos["symbol"])
            if price is None or price <= 0:
                continue
            if pos["side"] == "long":
                if price > pos["best_price"]:
                    pos["best_price"] = price
                    if pos.get("trail_offset"):
                        # Arm the trail if we've moved > trail_offset in favor
                        if not pos["trail_active"] and price >= pos["entry_price"] + pos["trail_offset"]:
                            pos["trail_active"] = True
                        if pos["trail_active"]:
                            new_sl = pos["best_price"] - pos["trail_offset"]
                            if pos["stop_loss"] is None or new_sl > pos["stop_loss"]:
                                pos["stop_loss"] = new_sl
                if pos["stop_loss"] is not None and price <= pos["stop_loss"]:
                    to_close.append((vid, pos["stop_loss"], "stop_loss"))
            else:  # short
                if price < pos["best_price"]:
                    pos["best_price"] = price
                    if pos.get("trail_offset"):
                        if not pos["trail_active"] and price <= pos["entry_price"] - pos["trail_offset"]:
                            pos["trail_active"] = True
                        if pos["trail_active"]:
                            new_sl = pos["best_price"] + pos["trail_offset"]
                            if pos["stop_loss"] is None or new_sl < pos["stop_loss"]:
                                pos["stop_loss"] = new_sl
                if pos["stop_loss"] is not None and price >= pos["stop_loss"]:
                    to_close.append((vid, pos["stop_loss"], "stop_loss"))
        for vid, exit_price, reason in to_close:
            self._close_internal(vid, exit_price, reason)
        if to_close:
            self._save()
        return len(to_close)

    def close_all(self, symbol: str, exit_price: float, reason: str) -> int:
        """Close all open positions for a symbol. Used on signal reversal."""
        to_close = [
            (vid, exit_price, reason)
            for vid, pos in list(self._state["open_positions"].items())
            if pos["symbol"] == symbol
        ]
        for vid, ep, r in to_close:
            self._close_internal(vid, ep, r)
        if to_close:
            self._save()
        return len(to_close)

    def _close_internal(self, vid: str, exit_price: float, reason: str) -> None:
        pos = self._state["open_positions"].pop(vid, None)
        if pos is None:
            return
        if pos["side"] == "long":
            pnl = (exit_price - pos["entry_price"]) * pos["size"]
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["size"]
        # Commission on both sides of the trade
        pnl -= pos["size_usd"] * COMMISSION_RATE * 2
        trade = {
            **pos,
            "exit_price": float(exit_price),
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": reason,
            "pnl": float(pnl),
        }
        self._state["closed_trades"].append(trade)
        self._state["balance"] += pnl

    def stats(self) -> dict:
        closed = self._state["closed_trades"]
        balance = self._state.get("balance", self.starting_balance)
        open_count = len(self._state["open_positions"])
        if not closed:
            return {
                "n": 0,
                "open": open_count,
                "balance": balance,
                "total_pnl": balance - self.starting_balance,
                "wr": 0.0,
                "avg_pnl": 0.0,
            }
        pnls = [t["pnl"] for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else float("inf")
        return {
            "n": len(closed),
            "open": open_count,
            "wins": len(wins),
            "losses": len(losses),
            "wr": 100 * len(wins) / len(pnls),
            "pf": pf,
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
            "balance": balance,
        }
