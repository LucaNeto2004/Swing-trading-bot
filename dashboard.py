"""
Trading Command Center — Swing Trading Bot dashboard.

Read-only Flask app. Reads paper_state.json + risk_state.json from disk,
pulls live prices directly from HL, serves a unified single-page view
at http://localhost:5070.

Does NOT run the trading loop — that lives in main.py.
"""
import json
import os
import sys as _sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from flask import Flask, jsonify, render_template

from config.deployer import load_all
from config.settings import load_config
from utils.logger import setup_logger

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_STATE_FILE = os.path.join(_BASE_DIR, "data", "paper_state.json")
RISK_STATE_FILE = os.path.join(_BASE_DIR, "data", "risk_state.json")

_SHARED_DIR = os.path.abspath(os.path.join(_BASE_DIR, "..", "shared"))
if _SHARED_DIR not in _sys.path:
    _sys.path.insert(0, _SHARED_DIR)
try:
    import vault_writer  # type: ignore
except Exception:
    vault_writer = None  # type: ignore

log = setup_logger("dashboard")

app = Flask(__name__, template_folder="templates", static_folder="static")

HL_API = "https://api.hyperliquid.xyz/info"
REFRESH_SECONDS = 15

state: dict[str, Any] = {
    "mode": "PAPER",
    "network": "MAINNET",
    "last_refresh": None,
    "balance": 0.0,
    "starting_balance": 0.0,
    "daily_pnl": 0.0,
    "open_count": 0,
    "kill_switch": False,
    "account_dd_halt": False,
    "consecutive_loss_halt": False,
    "consecutive_losses": 0,
    "account_peak_balance": 0.0,
    "effective_leverage": 6.0,
    "max_concurrent": 2,
    "positions": [],
    "trade_history": [],
    "symbols": [],
    "vault_recent": [],
    "prices": {},
}


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"failed to load {path}: {e}")
        return {}


def _fetch_mid_prices(symbols: list[str]) -> dict[str, float]:
    """Single HL call to pull mid prices for all symbols at once."""
    try:
        r = requests.post(HL_API, json={"type": "allMids"}, timeout=8)
        if r.status_code != 200:
            return {}
        all_mids = r.json() or {}
        return {s: float(all_mids.get(s, 0)) for s in symbols if all_mids.get(s)}
    except Exception as e:
        log.debug(f"mid price fetch error: {e}")
        return {}


def _unrealized(pos: dict, live_price: float) -> float:
    if not live_price or live_price <= 0:
        return 0.0
    if pos["side"] == "long":
        return (live_price - pos["entry_price"]) * pos["size"]
    return (pos["entry_price"] - live_price) * pos["size"]


def refresher():
    """Background loop that refreshes dashboard state every REFRESH_SECONDS."""
    config = load_config()
    deployed = load_all()
    state["mode"] = "PAPER" if config.paper_trading else "LIVE"
    state["network"] = "TESTNET" if config.testnet else "MAINNET"
    state["effective_leverage"] = config.sizing.effective_leverage
    state["max_concurrent"] = config.risk.max_concurrent_positions
    state["starting_balance"] = config.sizing.account_size

    symbol_list = list(config.instruments.keys())

    while True:
        try:
            paper = _load_json(PAPER_STATE_FILE)
            risk = _load_json(RISK_STATE_FILE)
            prices = _fetch_mid_prices(symbol_list)

            balance = float(paper.get("balance", config.sizing.account_size))
            starting = float(paper.get("starting_balance", config.sizing.account_size))
            positions_raw = paper.get("positions", {})

            pos_rows = []
            for sym, p in positions_raw.items():
                live = prices.get(sym, 0)
                unrl = _unrealized(p, live)
                dist_to_sl = ((live - p["sl"]) / live * 100) if live and p["side"] == "long" else \
                             ((p["sl"] - live) / live * 100) if live else 0
                pos_rows.append({
                    "symbol": sym,
                    "side": p["side"],
                    "entry": p["entry_price"],
                    "size": p["size"],
                    "notional": p["notional"],
                    "sl": p["sl"],
                    "tp1": p.get("tp1"),
                    "tp1_hit": p.get("tp1_hit", False),
                    "trail_active": p.get("trail_active", False),
                    "bars_held": p.get("bars_held", 0),
                    "max_hold_bars": p.get("max_hold_bars", 288),
                    "live": live,
                    "unrealized": unrl,
                    "dist_to_sl_pct": dist_to_sl,
                    "cfg_label": p.get("cfg_label", ""),
                })

            symbol_cards = []
            for sym in symbol_list:
                dep = deployed.get(sym, {})
                symbol_cards.append({
                    "symbol": sym,
                    "direction": dep.get("direction", "?"),
                    "entry_type": dep.get("entry_type", "?"),
                    "price": prices.get(sym, 0),
                    "backtest_pf": dep.get("backtest_pf"),
                    "backtest_pnl": dep.get("backtest_pnl"),
                    "backtest_trades": dep.get("backtest_trades"),
                    "in_position": sym in positions_raw,
                })

            vault_recent = []
            if vault_writer is not None:
                try:
                    vault_recent = vault_writer.list_recent("trades", limit=10) or []
                except Exception as e:
                    log.debug(f"vault recent failed: {e}")

            state.update({
                "last_refresh": datetime.now(timezone.utc).isoformat(),
                "balance": balance,
                "starting_balance": starting,
                "daily_pnl": float(risk.get("daily_pnl", 0.0)),
                "open_count": len(positions_raw),
                "kill_switch": bool(risk.get("kill_switch", False)),
                "account_dd_halt": bool(risk.get("account_dd_halt", False)),
                "consecutive_loss_halt": bool(risk.get("consecutive_loss_halt", False)),
                "consecutive_losses": int(risk.get("consecutive_losses", 0)),
                "account_peak_balance": float(risk.get("account_peak_balance", 0.0)),
                "positions": pos_rows,
                "symbols": symbol_cards,
                "vault_recent": vault_recent,
                "prices": prices,
            })
        except Exception as e:
            log.error(f"refresher error: {e}", exc_info=True)
        time.sleep(REFRESH_SECONDS)


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/healthz")
def health():
    last = state.get("last_refresh")
    ok = last is not None
    return jsonify({"ok": ok, "last_refresh": last})


def main():
    threading.Thread(target=refresher, daemon=True).start()
    port = int(os.environ.get("DASHBOARD_PORT", "5070"))
    log.info(f"Dashboard serving on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
