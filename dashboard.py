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
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import requests
from flask import Flask, jsonify, render_template

from config.deployer import load_all
from config.settings import load_config
from core.data import fetch_candles
from core.features import (
    add_features, last_pivot_levels_lookup_1h,
    trend_lookup_1h, structure_lookup_1h, hma_slope_lookup_1h,
    sjm_lookup_1h, kalman_slope_lookup_1h,
)
from core.quant_filters import rolling_hurst, compute_adx
from utils.logger import setup_logger

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_STATE_FILE = os.path.join(_BASE_DIR, "data", "paper_state.json")
RISK_STATE_FILE = os.path.join(_BASE_DIR, "data", "risk_state.json")

_SHARED_DIR = os.path.join(_BASE_DIR, "shared")
if not os.path.isdir(_SHARED_DIR):
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
REFRESH_SECONDS = 2  # tight so live prices tick on the UI

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
    "margin_pct": 0.15,
    "set_leverage": 40,
    "commission_pct": 0.0,
    "positions": [],
    "trade_history": [],
    "symbols": [],
    "vault_recent": [],
    "prices": {},
    "ticker": [],
    # Derived series / aggregates
    "equity_curve": [],       # [{ts, balance}]
    "mtm_history": [],        # [{ts, balance}] — 1-per-60s rolling 24h mark-to-market snapshot
    "daily_pnl_series": [],   # [{date, pnl, trades}]
    "attribution_symbol": [],
    "attribution_exit": [],
    "stats": {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "profit_factor": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
    },
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


_last_mid_cache: dict[str, float] = {}
_bos_cache: dict[str, Any] = {"data": {}, "ts": 0.0}

# Rolling mark-to-market history (balance + unrealized). Sampled on a
# regular 60s cadence so the v2 24h chart has dense, evenly-spaced points
# between trade closes. Persisted to disk so a dashboard restart doesn't
# erase today's curve.
MTM_HISTORY_FILE = os.path.join(_BASE_DIR, "data", "mtm_history.json")
# 10s cadence so the persisted series has the same visual smoothness as
# the in-tab 1.5s live tail — no visible density jump at the right edge.
# 24h × 360 samples/hr = 8640 samples max per day (~350KB on disk).
MTM_SNAPSHOT_S = 10
MTM_KEEP_HOURS = 25


def _load_mtm_history() -> list:
    try:
        if os.path.exists(MTM_HISTORY_FILE):
            with open(MTM_HISTORY_FILE) as f:
                return json.load(f) or []
    except Exception as e:
        log.warning(f"mtm_history load: {e}")
    return []


def _save_mtm_history(history: list) -> None:
    try:
        tmp = MTM_HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, separators=(",", ":"))
        os.replace(tmp, MTM_HISTORY_FILE)
    except Exception as e:
        log.warning(f"mtm_history save: {e}")


_mtm_history: list = _load_mtm_history()
_mtm_last_write_s: float = 0.0


def _classify_regime(up_cnt: int, dn_cnt: int, hurst: float, adx_v: float) -> str:
    if np.isnan(hurst) or np.isnan(adx_v):
        hurst = 0.5; adx_v = 20.0
    vote = up_cnt - dn_cnt
    if vote >= 2 and (hurst > 0.5 or adx_v > 18):
        return "trend_up"
    if vote <= -2 and (hurst > 0.5 or adx_v > 18):
        return "trend_down"
    if abs(vote) <= 2 and hurst < 0.5 and adx_v < 25:
        return "range"
    return "chop"


def _fetch_bos_levels(symbols: list[str]) -> dict[str, dict]:
    """Per-symbol BOS pivot levels + regime label.

    Cached 60s. Each sym entry: {bos_long, bos_short, price, regime,
    hurst, adx, ens_up, ens_dn}."""
    now = time.time()
    if now - _bos_cache["ts"] < 60 and _bos_cache["data"]:
        return _bos_cache["data"]
    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            d5 = fetch_candles(sym, "5m", 200)
            d1 = fetch_candles(sym, "1h", 200)
            if d5.empty or d1.empty:
                continue
            d5 = add_features(d5); d1 = add_features(d1)
            ph_arr, pl_arr = last_pivot_levels_lookup_1h(d5, d1, lookback=3)
            ph = float(ph_arr[-1]) if len(ph_arr) and not np.isnan(ph_arr[-1]) else None
            pl = float(pl_arr[-1]) if len(pl_arr) and not np.isnan(pl_arr[-1]) else None
            price = float(d5["close"].iloc[-1])
            # Regime — Hurst + ADX + 5-filter ensemble vote
            hurst_raw = rolling_hurst(d1["close"].to_numpy(), window=100)
            adx_raw = compute_adx(d1["high"].to_numpy(), d1["low"].to_numpy(),
                                  d1["close"].to_numpy(), period=14)
            hurst = float(hurst_raw[-1]) if not np.isnan(hurst_raw[-1]) else float("nan")
            adx_v = float(adx_raw[-1]) if not np.isnan(adx_raw[-1]) else float("nan")
            up_e, dn_e = trend_lookup_1h(d5, d1)
            up_s, dn_s = structure_lookup_1h(d5, d1)
            up_h, dn_h = hma_slope_lookup_1h(d5, d1)
            up_j, dn_j = sjm_lookup_1h(d5, d1)
            up_k, dn_k = kalman_slope_lookup_1h(d5, d1)
            up_cnt = int(up_e[-1]) + int(up_s[-1]) + int(up_h[-1]) + int(up_j[-1]) + int(up_k[-1])
            dn_cnt = int(dn_e[-1]) + int(dn_s[-1]) + int(dn_h[-1]) + int(dn_j[-1]) + int(dn_k[-1])
            regime = _classify_regime(up_cnt, dn_cnt, hurst, adx_v)
            out[sym] = {"bos_long": ph, "bos_short": pl, "price": price,
                        "regime": regime, "ens_up": up_cnt, "ens_dn": dn_cnt,
                        "hurst": round(hurst, 2) if not np.isnan(hurst) else None,
                        "adx": round(adx_v, 1) if not np.isnan(adx_v) else None}
        except Exception as e:
            log.debug(f"BOS fetch {sym}: {e}")
    if out:
        _bos_cache["data"] = out
        _bos_cache["ts"] = now
    return _bos_cache["data"] or out


def _fetch_xyz_mids(xyz_syms: list[str]) -> dict[str, float]:
    """allMids doesn't cover xyz HIP-3 deployer perps — pull last close via
    candleSnapshot per poll. The latest 1m candle's close updates in real time
    as trades print, so this matches the cadence of allMids for crypto."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 2 * 60_000  # just the last minute or two
    for sym in xyz_syms:
        try:
            r = requests.post(HL_API, json={
                "type": "candleSnapshot",
                "req": {"coin": sym, "interval": "1m",
                        "startTime": start_ms, "endTime": end_ms},
            }, timeout=5)
            if r.status_code != 200:
                continue
            data = r.json() or []
            if isinstance(data, list) and data:
                _last_mid_cache[sym] = float(data[-1].get("c", 0) or 0)
        except Exception as e:
            log.debug(f"xyz price fetch {sym}: {e}")
    return {s: _last_mid_cache[s] for s in xyz_syms if s in _last_mid_cache}


def _fetch_mid_prices(symbols: list[str]) -> dict[str, float]:
    """Single HL call to pull mid prices for all symbols at once.

    Falls back to the last-known price when HL's allMids briefly fails or
    returns a partial response — otherwise zeroed prices cause the dashboard's
    unrealized calc to collapse to 0 for a poll, producing spurious downward
    spikes on the 24h P&L chart.

    xyz HIP-3 deployer perps aren't in allMids; they go through a separate
    throttled candleSnapshot path."""
    try:
        r = requests.post(HL_API, json={"type": "allMids"}, timeout=8)
        if r.status_code != 200:
            fresh = {}
        else:
            all_mids = r.json() or {}
            fresh = {s: float(all_mids.get(s, 0)) for s in symbols if all_mids.get(s)}
    except Exception as e:
        log.debug(f"mid price fetch error: {e}")
        fresh = {}
    _last_mid_cache.update(fresh)
    xyz_syms = [s for s in symbols if s.startswith("xyz:")]
    if xyz_syms:
        _fetch_xyz_mids(xyz_syms)
    return {s: _last_mid_cache[s] for s in symbols if s in _last_mid_cache}


_ticker_cache: dict[str, Any] = {"data": [], "ts": 0.0}


def _fetch_ticker_data(symbols: list[str]) -> list[dict]:
    """Build the top-strip ticker: mark, 24h change %, 24h notional volume.

    Uses HL's metaAndAssetCtxs which returns prevDayPx + dayNtlVlm in one call.
    Throttled to 15s — ticker values don't need sub-second updates and this
    payload is bigger than allMids. Missing symbols (e.g. xyz HIP-3 perps) fall
    back to price-only using the last-known mid so they still appear."""
    now = time.time()
    if now - _ticker_cache["ts"] < 15 and _ticker_cache["data"]:
        return _ticker_cache["data"]
    by_name: dict[str, dict] = {}
    try:
        r = requests.post(HL_API, json={"type": "metaAndAssetCtxs"}, timeout=8)
        if r.status_code == 200:
            data = r.json() or []
            if isinstance(data, list) and len(data) >= 2:
                universe = (data[0] or {}).get("universe", []) or []
                ctxs = data[1] or []
                for i, asset in enumerate(universe):
                    if i >= len(ctxs):
                        break
                    name = (asset or {}).get("name")
                    c = ctxs[i] or {}
                    if not name:
                        continue
                    try:
                        mark = float(c.get("markPx") or 0)
                        prev = float(c.get("prevDayPx") or 0)
                        vol = float(c.get("dayNtlVlm") or 0)
                        chg = ((mark - prev) / prev * 100.0) if prev else 0.0
                        by_name[name] = {"price": mark, "change_pct": chg, "volume": vol}
                    except (TypeError, ValueError):
                        continue
    except Exception as e:
        log.debug(f"ticker fetch error: {e}")

    out = []
    for sym in symbols:
        info = by_name.get(sym)
        if info and info["price"] > 0:
            out.append({
                "symbol": sym,
                "price": info["price"],
                "change_pct": info["change_pct"],
                "volume": info["volume"],
            })
        elif _last_mid_cache.get(sym):
            # xyz HIP-3 perps aren't in metaAndAssetCtxs — show price w/o % change
            out.append({
                "symbol": sym,
                "price": _last_mid_cache[sym],
                "change_pct": None,
                "volume": None,
            })
    if out:
        _ticker_cache["data"] = out
        _ticker_cache["ts"] = now
    return _ticker_cache["data"] or out


def _unrealized(pos: dict, live_price: float) -> float:
    if not live_price or live_price <= 0:
        return 0.0
    if pos["side"] == "long":
        return (live_price - pos["entry_price"]) * pos["size"]
    return (pos["entry_price"] - live_price) * pos["size"]


def _parse_ts(ts: Any) -> Optional[datetime]:
    """Parse timestamp to NAIVE UTC datetime. Strips tzinfo so the whole ledger
    can be sorted/compared uniformly — trade entries historically mix naive
    (most) with tz-aware (any row written via datetime.now(timezone.utc))."""
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    except ValueError:
        try:
            return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def _compute_derived(
    trade_history: list[dict],
    starting_balance: float,
    active_symbols: Optional[set] = None,
) -> dict:
    """Equity curve, daily P&L series, attribution tables, aggregate stats.

    ``active_symbols`` (optional) — when provided, the attribution_symbol
    table is filtered to only include symbols in this set. Equity curve,
    daily series, and aggregate stats stay full-history (they describe
    overall account performance regardless of which symbols are still
    rotating). Use to hide retired symbols from per-symbol UI panels.
    """
    # Sort chronologically (entries may be unordered in JSON)
    trades = sorted(
        (t for t in trade_history if t.get("pnl") is not None),
        key=lambda t: _parse_ts(t.get("timestamp")) or datetime.min,
    )

    equity = []
    running = starting_balance
    equity.append({"ts": None, "balance": running, "label": "start"})
    for t in trades:
        running += float(t.get("pnl") or 0.0)
        equity.append({
            "ts": str(t.get("timestamp")),
            "balance": round(running, 2),
            "symbol": t.get("symbol"),
            "pnl": round(float(t.get("pnl") or 0.0), 2),
        })

    daily_bucket: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for t in trades:
        dt = _parse_ts(t.get("timestamp"))
        day = dt.date().isoformat() if dt else "unknown"
        pnl = float(t.get("pnl") or 0.0)
        b = daily_bucket[day]
        b["pnl"] += pnl
        b["trades"] += 1
        if pnl >= 0:
            b["wins"] += 1
        else:
            b["losses"] += 1
    daily_series = [
        {"date": d, "pnl": round(v["pnl"], 2), "trades": v["trades"],
         "wins": v["wins"], "losses": v["losses"]}
        for d, v in sorted(daily_bucket.items())
    ]

    def _bucket(key: str) -> list[dict]:
        buckets: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "wins": 0, "losses": 0,
                                                        "gross_win": 0.0, "gross_loss": 0.0})
        for t in trades:
            k = t.get(key) or "unknown"
            pnl = float(t.get("pnl") or 0.0)
            b = buckets[k]
            b["pnl"] += pnl
            if pnl >= 0:
                b["wins"] += 1
                b["gross_win"] += pnl
            else:
                b["losses"] += 1
                b["gross_loss"] += -pnl
        rows = []
        for k, v in buckets.items():
            total = v["wins"] + v["losses"]
            wr = (v["wins"] / total * 100) if total else 0
            pf = (v["gross_win"] / v["gross_loss"]) if v["gross_loss"] > 0 else None
            rows.append({
                "key": k, "pnl": round(v["pnl"], 2), "trades": total,
                "win_rate": round(wr, 1),
                "profit_factor": round(pf, 2) if pf is not None else None,
            })
        rows.sort(key=lambda r: r["pnl"], reverse=True)
        return rows

    # Aggregate stats
    wins = [float(t.get("pnl") or 0.0) for t in trades if float(t.get("pnl") or 0.0) > 0]
    losses = [float(t.get("pnl") or 0.0) for t in trades if float(t.get("pnl") or 0.0) < 0]
    total = len(trades)
    total_pnl = sum(float(t.get("pnl") or 0.0) for t in trades)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    stats = {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "total_pnl": round(total_pnl, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best_trade": round(max(wins), 2) if wins else 0.0,
        "worst_trade": round(min(losses), 2) if losses else 0.0,
    }

    attrib_sym = _bucket("symbol")
    if active_symbols is not None:
        attrib_sym = [r for r in attrib_sym if r["key"] in active_symbols]

    return {
        "equity_curve": equity,
        "daily_pnl_series": daily_series,
        "attribution_symbol": attrib_sym,
        "attribution_exit": _bucket("exit_reason"),
        "stats": stats,
    }


def refresher():
    """Background loop that refreshes dashboard state every REFRESH_SECONDS."""
    config = load_config()
    deployed = load_all()
    state["mode"] = "PAPER" if config.paper_trading else "LIVE"
    state["network"] = "TESTNET" if config.testnet else "MAINNET"
    # Effective leverage is now per-symbol — this is the cap (BTC-tier)
    state["effective_leverage"] = config.sizing.effective_leverage
    state["max_concurrent"] = config.risk.max_concurrent_positions
    state["starting_balance"] = config.sizing.account_size
    state["margin_pct"] = config.sizing.margin_pct
    state["set_leverage"] = config.sizing.set_leverage
    state["commission_pct"] = config.risk.commission_pct
    state["per_symbol_leverage"] = {
        sym: {
            "hl_max_leverage": inst.hl_max_leverage,
            "set_leverage": min(config.sizing.set_leverage, inst.hl_max_leverage),
            "effective_leverage": round(
                config.sizing.margin_pct * min(config.sizing.set_leverage, inst.hl_max_leverage), 2
            ),
        }
        for sym, inst in config.instruments.items()
    }

    symbol_list = list(config.instruments.keys())

    while True:
        try:
            paper = _load_json(PAPER_STATE_FILE)
            risk = _load_json(RISK_STATE_FILE)
            prices = _fetch_mid_prices(symbol_list)
            ticker = _fetch_ticker_data(symbol_list)

            balance = float(paper.get("balance", config.sizing.account_size))
            starting = float(paper.get("starting_balance", config.sizing.account_size))
            positions_raw = paper.get("positions", {})
            trade_history_raw = paper.get("trade_history", []) or []

            pos_rows = []
            for sym, p in positions_raw.items():
                live = prices.get(sym, 0)
                unrl = _unrealized(p, live)
                dist_to_sl = ((live - p["sl"]) / live * 100) if live and p["side"] == "long" else \
                             ((p["sl"] - live) / live * 100) if live else 0
                pos_lev = int(p.get("set_leverage", config.sizing.set_leverage))
                pos_eff_lev = config.sizing.margin_pct * pos_lev
                # Synthetic liq price for paper positions. HL's actual
                # maintenance margin varies per asset tier (~0.5% for
                # BTC/ETH, ~2% for most majors, ~5%+ for low-cap); we use
                # 2% as a realistic default. When the bot goes live, swap
                # this for `liquidationPx` from HL's clearinghouseState.
                MM_RATE = 0.02
                entry_px = p["entry_price"]
                if pos_eff_lev and entry_px:
                    if p["side"] == "long":
                        liq_price = entry_px * (1 - 1.0 / pos_eff_lev + MM_RATE)
                    else:
                        liq_price = entry_px * (1 + 1.0 / pos_eff_lev - MM_RATE)
                    liq_price = max(0.0, liq_price)
                    liq_dist_pct = abs(live - liq_price) / live * 100 if live else None
                else:
                    liq_price = None
                    liq_dist_pct = None
                pos_rows.append({
                    "symbol": sym,
                    "side": p["side"],
                    "entry": p["entry_price"],
                    "size": p["size"],
                    "notional": p["notional"],
                    "sl": p["sl"],
                    "tp1": p.get("tp1"),
                    "tp1_hit": p.get("tp1_hit", False),
                    "tp2": p.get("tp2"),
                    "tp2_hit": p.get("tp2_hit", False),
                    "tp3": p.get("tp3"),
                    "tp3_hit": p.get("tp3_hit", False),
                    "trail_active": p.get("trail_active", False),
                    "trail_offset": p.get("trail_offset", 0.0),
                    "best_price": p.get("best_price", p["entry_price"]),
                    "bars_held": p.get("bars_held", 0),
                    "max_hold_bars": p.get("max_hold_bars", 288),
                    "live": live,
                    "unrealized": unrl,
                    "dist_to_sl_pct": dist_to_sl,
                    "cfg_label": p.get("cfg_label", ""),
                    "set_leverage": pos_lev,
                    "effective_leverage": round(pos_eff_lev, 2),
                    "liq_price": round(liq_price, 4) if liq_price is not None else None,
                    "liq_distance_pct": round(liq_dist_pct, 1) if liq_dist_pct is not None else None,
                })

            bos_map = _fetch_bos_levels(symbol_list)
            symbol_cards = []
            for sym in symbol_list:
                dep = deployed.get(sym, {})
                inst = config.instruments.get(sym)
                sym_max_lev = inst.hl_max_leverage if inst is not None else config.sizing.set_leverage
                sym_set_lev = min(config.sizing.set_leverage, sym_max_lev)
                sym_eff_lev = config.sizing.margin_pct * sym_set_lev
                price_now = prices.get(sym, 0)
                bos = bos_map.get(sym, {})
                bos_long = bos.get("bos_long")
                bos_short = bos.get("bos_short")
                dist_long = ((bos_long - price_now) / price_now * 100.0
                             if bos_long and price_now else None)
                dist_short = ((bos_short - price_now) / price_now * 100.0
                              if bos_short and price_now else None)
                symbol_cards.append({
                    "symbol": sym,
                    "direction": dep.get("direction", "?"),
                    "entry_type": dep.get("entry_type", "?"),
                    "exit_type": dep.get("exit_type", "standard"),
                    "ensemble_k": dep.get("ensemble_k"),
                    "use_1h_filter": dep.get("use_1h_filter"),
                    "trend_filter_1h": dep.get("trend_filter_1h"),
                    "sl_atr": dep.get("sl_atr"),
                    "tp1_atr": dep.get("tp1_atr"),
                    "tp1_pct": dep.get("tp1_pct"),
                    "tp2_atr": dep.get("tp2_atr"),
                    "tp2_pct": dep.get("tp2_pct"),
                    "tp3_atr": dep.get("tp3_atr"),
                    "tp3_pct": dep.get("tp3_pct"),
                    "trail_atr": dep.get("trail_atr"),
                    "max_hold_bars": dep.get("max_hold_bars"),
                    "price": price_now,
                    "backtest_pf": dep.get("backtest_pf"),
                    "backtest_pnl": dep.get("backtest_pnl"),
                    "backtest_trades": dep.get("backtest_trades"),
                    "backtest_p_win": dep.get("backtest_p_win"),
                    "in_position": sym in positions_raw,
                    "hl_max_leverage": sym_max_lev,
                    "set_leverage": sym_set_lev,
                    "effective_leverage": round(sym_eff_lev, 2),
                    "bos_long": bos_long,
                    "bos_short": bos_short,
                    "bos_long_dist_pct": round(dist_long, 2) if dist_long is not None else None,
                    "bos_short_dist_pct": round(dist_short, 2) if dist_short is not None else None,
                    "regime": bos.get("regime"),
                    "ens_up": bos.get("ens_up"),
                    "ens_dn": bos.get("ens_dn"),
                    "hurst": bos.get("hurst"),
                    "adx": bos.get("adx"),
                })

            vault_recent = []
            if vault_writer is not None:
                try:
                    vault_recent = vault_writer.list_recent("trades", limit=12) or []
                except Exception as e:
                    log.debug(f"vault recent failed: {e}")

            # CRITICAL: equity_curve / daily_series / total_pnl must use the
            # FULL trade history (including retired symbols' losses) so the
            # chart baseline matches actual balance. Filtering at the input
            # would build a counterfactual trajectory (what balance would
            # have been if we'd never traded retired symbols) — that breaks
            # the 24h chart, since the baseline doesn't match real balance.
            #
            # Per-symbol attribution IS filtered via `active_symbols`. The
            # visible trade table below ALSO filters via `deployed_syms` —
            # but only AFTER equity_curve has been built from full data.
            deployed_syms = set(deployed.keys())
            derived = _compute_derived(trade_history_raw, starting, active_symbols=deployed_syms)
            trade_history_filtered = [t for t in trade_history_raw
                                      if t.get("symbol") in deployed_syms]

            trade_rows = []
            cumulative = 0.0
            sorted_trades = sorted(
                trade_history_filtered,
                key=lambda t: _parse_ts(t.get("timestamp")) or datetime.min,
            )
            for i, t in enumerate(sorted_trades, 1):
                pnl = float(t.get("pnl") or 0.0)
                cumulative += pnl
                trade_rows.append({
                    "n": i,
                    "timestamp": str(t.get("timestamp") or ""),
                    "symbol": t.get("symbol"),
                    "side": t.get("side"),
                    "size": t.get("size"),
                    "price": t.get("price"),
                    "notional": t.get("notional"),
                    "pnl": pnl,
                    "exit_reason": t.get("exit_reason"),
                    "held_bars": t.get("held_bars", 0),
                    "runner_r": t.get("runner_r"),
                    "favorable_excursion_atr": t.get("favorable_excursion_atr"),
                    "entry_price": t.get("entry_price"),
                    "initial_sl": t.get("initial_sl"),
                    "r": t.get("r"),
                    "adverse_excursion_atr": t.get("adverse_excursion_atr"),
                    "cumulative": round(cumulative, 2),
                })

            # Sample live equity (realized + open unrealized) every 60s and
            # persist. This gives the v2 24h chart ~1440 evenly-spaced points
            # across the day instead of sparse trade-close-only equity_curve
            # data — the cause of the big angular gaps between trades.
            global _mtm_last_write_s, _mtm_history
            live_eq_now = balance + sum(
                float(p.get("unrealized", 0.0)) for p in pos_rows
            )
            now_s = time.time()
            if now_s - _mtm_last_write_s >= MTM_SNAPSHOT_S:
                _mtm_history.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "balance": round(live_eq_now, 6),
                })
                # Trim anything older than the retention window.
                cutoff = now_s - MTM_KEEP_HOURS * 3600
                kept = []
                for h in _mtm_history:
                    try:
                        t = datetime.fromisoformat(str(h["ts"]).replace("Z", "+00:00"))
                        if t.timestamp() >= cutoff:
                            kept.append(h)
                    except Exception:
                        continue
                _mtm_history = kept
                _mtm_last_write_s = now_s
                _save_mtm_history(_mtm_history)

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
                "trade_history": trade_rows,
                "symbols": symbol_cards,
                "vault_recent": vault_recent,
                "prices": prices,
                "ticker": ticker,
                "mtm_history": list(_mtm_history),
                **derived,
            })
        except Exception as e:
            log.error(f"refresher error: {e}", exc_info=True)
        time.sleep(REFRESH_SECONDS)


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/v2")
def index_v2():
    return app.send_static_file("v2/DASHBAORD.html")


@app.route("/v2/charts")
def v2_charts():
    return app.send_static_file("v2/CHARTS.html")


@app.route("/v2/signals")
def v2_signals():
    return app.send_static_file("v2/SIGNALS.html")


@app.route("/v2/journal")
def v2_journal():
    return app.send_static_file("v2/JOURNAL.html")


@app.route("/v2/risk")
def v2_risk():
    return app.send_static_file("v2/RISK.html")


@app.route("/v2/strategies")
def v2_strategies():
    return app.send_static_file("v2/STRATEGIES.html")


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/api/signals")
def api_signals():
    """Read the tail of logs/signals.jsonl (written by main.py's cycle loop).
    Returns newest-first. Empty list if the file doesn't exist yet."""
    path = os.path.join(_BASE_DIR, "logs", "signals.jsonl")
    if not os.path.exists(path):
        return jsonify({"events": []})
    try:
        with open(path) as f:
            lines = f.readlines()
        events = []
        for ln in lines[-500:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                events.append(json.loads(ln))
            except Exception:
                continue
        events.reverse()  # newest first
        return jsonify({"events": events})
    except Exception as e:
        return jsonify({"events": [], "error": str(e)}), 500


@app.route("/api/chart_data/<path:symbol>")
def api_chart_data(symbol: str):
    """Proxy to chart_server.py's /api/data/<sym> so the v2 Charts tab can
    reuse the rich overlay data (pivots / valid pivots / regime / BOS levels
    / position) already computed by the bot pipeline — without CORS noise.
    Runs even when chart_server is down; just returns a stub so the tile can
    fall back to HL candles."""
    try:
        r = requests.get(f"http://localhost:5080/api/data/{symbol}", timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": f"chart_server unreachable: {e}"}), 502


@app.route("/healthz")
def health():
    last = state.get("last_refresh")
    ok = last is not None
    return jsonify({"ok": ok, "last_refresh": last})


def _prime_state_from_disk():
    """Bootstrap state.balance + starting_balance from paper_state.json at
    startup so the first few /api/state polls don't return 0 (which the
    frontend plots as a giant negative P&L spike until the refresher catches up)."""
    try:
        paper = _load_json(PAPER_STATE_FILE)
        config = load_config()
        state["starting_balance"] = float(paper.get("starting_balance", config.sizing.account_size))
        state["balance"] = float(paper.get("balance", state["starting_balance"]))
    except Exception as e:
        log.warning(f"couldn't prime state at startup: {e}")


def main():
    _prime_state_from_disk()
    threading.Thread(target=refresher, daemon=True).start()
    port = int(os.environ.get("DASHBOARD_PORT", "5070"))
    log.info(f"Dashboard serving on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
