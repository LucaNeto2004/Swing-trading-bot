"""Interactive + static chart server for the swing bot.

Interactive (Plotly) — browser: http://localhost:5080
    Grid of live candlestick charts with hover crosshair, zoom, pan.
    Overlays EMA9/21/50, BB bands, and — for open positions — entry/SL/TP
    lines. Auto-refreshes (30s open positions, 5min idle).

Static PNGs — for the future LLM advisor + Discord attachments
    Still written to data/charts/<SYMBOL>_5m.png by ``snapshot_chart.render``.

Run:
    python scripts/chart_server.py
    CHART_PORT=... python scripts/chart_server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless — must set before any pyplot import

import plotly.graph_objects as go
from flask import Flask, abort, jsonify, send_from_directory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from config.settings import load_config  # noqa: E402
from config.deployer import load_all as load_all_deployed  # noqa: E402
from core.data import fetch_candles  # noqa: E402
from core.features import (  # noqa: E402
    add_features, trend_lookup_1h, structure_lookup_1h,
    hma_slope_lookup_1h, sjm_lookup_1h, kalman_slope_lookup_1h,
)
from snapshot_chart import render as render_chart_png  # noqa: E402


def _active_filter_for(symbol: str) -> tuple[str, bool]:
    """Return (filter_name, require_4h) for a symbol from its deployed config."""
    try:
        dep = load_all_deployed().get(symbol, {})
        return (dep.get("trend_filter_1h", "ema_cross"),
                bool(dep.get("require_4h_agreement", False)))
    except Exception:
        return "ema_cross", False


def _regime_arrays(filter_variant: str, df_5m, df_1h):
    """(up, dn) per 5m bar under the active filter."""
    if filter_variant == "structure":
        return structure_lookup_1h(df_5m, df_1h)
    if filter_variant == "both_agree":
        up_e, dn_e = trend_lookup_1h(df_5m, df_1h)
        up_s, dn_s = structure_lookup_1h(df_5m, df_1h)
        return up_e & up_s, dn_e & dn_s
    if filter_variant == "hma_slope":
        return hma_slope_lookup_1h(df_5m, df_1h)
    if filter_variant == "sjm":
        return sjm_lookup_1h(df_5m, df_1h)
    if filter_variant == "kalman":
        return kalman_slope_lookup_1h(df_5m, df_1h)
    return trend_lookup_1h(df_5m, df_1h)


def _regime_segments(ts, up_arr, dn_arr):
    """Collapse per-bar regime into (start_ts, end_ts, state) tuples.
    state ∈ {'up','dn','neutral'}."""
    n = len(up_arr)
    if n == 0:
        return []
    def state_of(i):
        if up_arr[i] and not dn_arr[i]: return "up"
        if dn_arr[i] and not up_arr[i]: return "dn"
        return "neutral"
    segs = []
    cur = state_of(0); start = 0
    for i in range(1, n):
        s = state_of(i)
        if s != cur:
            segs.append((ts.iloc[start], ts.iloc[i], cur))
            cur = s; start = i
    segs.append((ts.iloc[start], ts.iloc[n - 1], cur))
    return segs


def _pivots_on_1h(df_1h, lookback: int = 3):
    """Confirmed 1h swing pivots: (highs, lows) as lists of (ts, price)."""
    if df_1h is None or df_1h.empty:
        return [], []
    highs = df_1h["high"].to_numpy()
    lows = df_1h["low"].to_numpy()
    ts = df_1h["timestamp"].to_numpy()
    n = len(df_1h)
    pivot_highs = []
    pivot_lows = []
    for i in range(lookback, n - lookback):
        wh = highs[i - lookback: i + lookback + 1]
        wl = lows[i - lookback: i + lookback + 1]
        if highs[i] == wh.max() and (wh == highs[i]).sum() == 1:
            pivot_highs.append((ts[i], float(highs[i])))
        if lows[i] == wl.min() and (wl == lows[i]).sum() == 1:
            pivot_lows.append((ts[i], float(lows[i])))
    return pivot_highs, pivot_lows

CHARTS_DIR = REPO_ROOT / "data" / "charts"
PAPER_STATE = REPO_ROOT / "data" / "paper_state.json"

app = Flask(__name__)
_render_lock = threading.Lock()


def _load_state() -> dict:
    try:
        with open(PAPER_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _open_symbols() -> list[str]:
    return list((_load_state().get("positions") or {}).keys())


def _render_png(symbol: str, interval: str = "5m") -> bool:
    with _render_lock:
        try:
            return render_chart_png(symbol, interval=interval) is not None
        except Exception as exc:
            app.logger.warning(f"PNG render {symbol} {interval} failed: {exc}")
            return False


CHART_BARS = 600            # 5m bars kept in memory (~50h of history)
INITIAL_VIEW_BARS = 130     # bars visible at first paint (~10.8h) — zoom out reveals the rest


def _build_plotly_figure(symbol: str, interval: str = "5m", bars: int = CHART_BARS) -> dict | None:
    raw = fetch_candles(symbol, interval, bars)
    if raw.empty:
        return None
    df = add_features(raw).tail(bars).reset_index(drop=True)
    # default (populated inside the try block below if 1h fetch succeeds)
    valid_pivot_highs = []
    valid_pivot_lows = []

    # 1h data — for regime ribbon + pivot overlay. Also pulled in bot pipeline.
    regime_segs = []
    pivot_highs = []
    pivot_lows = []
    bos_long_level = None   # price level a BOS long would trigger at (most recent pivot H)
    bos_short_level = None  # most recent pivot L
    filter_variant, require_4h = _active_filter_for(symbol)
    try:
        raw_1h = fetch_candles(symbol, "1h", 200)
        if not raw_1h.empty:
            df_1h = add_features(raw_1h)
            up_arr, dn_arr = _regime_arrays(filter_variant, df, df_1h)
            regime_segs = _regime_segments(df["timestamp"], up_arr, dn_arr)
            pivot_highs, pivot_lows = _pivots_on_1h(df_1h, lookback=3)
            # "Most recent confirmed pivot" — what BOS would break.
            # Filter to pivots confirmed BEFORE the last visible 5m bar
            # (pivot is confirmed `lookback` bars after its peak/trough).
            t0 = df["timestamp"].iloc[0]
            t1 = df["timestamp"].iloc[-1]
            if pivot_highs:
                bos_long_level = pivot_highs[-1][1]
            if pivot_lows:
                bos_short_level = pivot_lows[-1][1]
            # Only keep pivots inside the visible 5m window for markers
            pivot_highs = [(t, p) for t, p in pivot_highs if t0 <= t <= t1]
            pivot_lows = [(t, p) for t, p in pivot_lows if t0 <= t <= t1]

            # VALIDATED pivots — ones that pass the quant filters used by
            # pullback_in_regime (RSI extreme / BB touch / volume spike / ATR
            # move). Shown with a different, brighter marker.
            try:
                from core.quant_filters import combined_pivots_1h
                valid_h_list, valid_l_list = combined_pivots_1h(
                    df_1h, fractal_lookback=3, smoothed_lookback=2,
                    atr_min_move=1.0, vol_spike=1.3, rsi_extreme=(35.0, 65.0),
                )
                valid_pivot_highs = [
                    (pd.Timestamp(ct, unit="ns", tz="UTC"), lvl)
                    for ct, _, lvl, _ in valid_h_list
                    if t0 <= pd.Timestamp(ct, unit="ns", tz="UTC") <= t1
                ]
                valid_pivot_lows = [
                    (pd.Timestamp(ct, unit="ns", tz="UTC"), lvl)
                    for ct, _, lvl, _ in valid_l_list
                    if t0 <= pd.Timestamp(ct, unit="ns", tz="UTC") <= t1
                ]
            except Exception:
                valid_pivot_highs, valid_pivot_lows = [], []
    except Exception as exc:
        app.logger.warning(f"regime/pivots for {symbol} failed: {exc}")

    pos = (_load_state().get("positions") or {}).get(symbol)
    # Force explicit ISO-8601 UTC strings ("...Z") so the browser's Date parser
    # treats them as UTC. Plotly's default pandas serialization emits naive
    # nanosecond ISO strings which JS misreads as local time — that caused the
    # live-bar comparisons to drift by the browser's TZ offset and produced
    # flickering / duplicate bars.
    ts = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
    ts_first, ts_last = ts[0], ts[-1]

    fig = go.Figure()

    # Convert all numeric columns to plain Python lists so plotly's JSON
    # serializer emits them as normal JSON arrays (not the "bdata" binary blob).
    # Plain arrays let the client mutate the payload before Plotly.react —
    # needed to merge live WS bars into REST data in a single atomic paint.
    o_l = df["open"].tolist()
    h_l = df["high"].tolist()
    l_l = df["low"].tolist()
    c_l = df["close"].tolist()

    # Candles on yaxis (price)
    fig.add_trace(go.Candlestick(
        x=ts, open=o_l, high=h_l, low=l_l, close=c_l,
        increasing_line_color="#16a34a", decreasing_line_color="#dc2626",
        increasing_fillcolor="#16a34a", decreasing_fillcolor="#dc2626",
        name="price", yaxis="y", showlegend=False,
        hovertext=[
            f"O {o:.4f}<br>H {h:.4f}<br>L {l:.4f}<br>C {c:.4f}"
            for o, h, l, c in zip(o_l, h_l, l_l, c_l)
        ],
        hoverinfo="x+text",
    ))

    # Indicators on yaxis
    for col, color, name in (
        ("ema_9", "#fbbf24", "EMA9"),
        ("ema_21", "#60a5fa", "EMA21"),
        ("ema_50", "#f472b6", "EMA50"),
    ):
        fig.add_trace(go.Scatter(
            x=ts, y=df[col].tolist(), mode="lines", name=name,
            line=dict(color=color, width=1), yaxis="y",
            hovertemplate=f"{name}: %{{y:.4f}}<extra></extra>",
        ))
    for col, name in (("bb_upper", "BB upper"), ("bb_lower", "BB lower")):
        fig.add_trace(go.Scatter(
            x=ts, y=df[col].tolist(), mode="lines", name=name,
            line=dict(color="#6b7280", width=0.7, dash="dot"),
            yaxis="y", showlegend=False,
            hovertemplate=f"{name}: %{{y:.4f}}<extra></extra>",
        ))

    # RSI on yaxis2
    fig.add_trace(go.Scatter(
        x=ts, y=df["rsi"].tolist(), mode="lines", name="RSI",
        line=dict(color="#a78bfa", width=1), yaxis="y2",
        hovertemplate="RSI: %{y:.1f}<extra></extra>",
    ))

    # Horizontal reference lines — RSI 30/50/70 + entry/SL/TP (if open)
    shapes: list[dict] = []
    annotations: list[dict] = []

    # Regime ribbon — thin band in the gap between price pane (yaxis starts
    # at 0.26) and RSI pane (yaxis2 ends at 0.22). Fits entirely inside the
    # [0.22, 0.26] gap so candles are never clipped. Green=up, red=dn.
    _RIBBON = {"up": "rgba(34,197,94,0.55)", "dn": "rgba(239,68,68,0.55)",
               "neutral": "rgba(107,114,128,0.15)"}
    for seg_start, seg_end, state in regime_segs:
        shapes.append(dict(
            type="rect", xref="x", yref="paper",
            x0=seg_start, x1=seg_end, y0=0.228, y1=0.252,
            fillcolor=_RIBBON[state], line=dict(width=0),
            layer="below",
        ))

    # BOS trigger levels — horizontal dashed lines at the most recent confirmed
    # pivot H (BOS-long trigger) and pivot L (BOS-short trigger). When price
    # closes above the H line, a BOS-structural long would fire. Below the L
    # line → short. Shown on every symbol so you can see pre-trade targets.
    if bos_long_level is not None:
        shapes.append(dict(
            type="line", xref="x", yref="y",
            x0=ts_first, x1=ts_last,
            y0=bos_long_level, y1=bos_long_level,
            line=dict(color="#fbbf24", width=1.5, dash="dashdot"),
        ))
        annotations.append(dict(
            xref="paper", yref="y", x=0.01, y=bos_long_level, xanchor="left",
            text=f"BOS↑ @ {bos_long_level:.4f}",
            showarrow=False, font=dict(color="#fbbf24", size=10),
            bgcolor="rgba(11,15,23,0.75)",
        ))
    if bos_short_level is not None:
        shapes.append(dict(
            type="line", xref="x", yref="y",
            x0=ts_first, x1=ts_last,
            y0=bos_short_level, y1=bos_short_level,
            line=dict(color="#a78bfa", width=1.5, dash="dashdot"),
        ))
        annotations.append(dict(
            xref="paper", yref="y", x=0.01, y=bos_short_level, xanchor="left",
            text=f"BOS↓ @ {bos_short_level:.4f}",
            showarrow=False, font=dict(color="#a78bfa", size=10),
            bgcolor="rgba(11,15,23,0.75)",
        ))

    # 1h ICT pivot markers — confirmed swing highs/lows the structure filter
    # uses internally. Red triangles above pivot highs, green below pivot lows.
    if pivot_highs:
        fig.add_trace(go.Scatter(
            x=[t for t, _ in pivot_highs],
            y=[p for _, p in pivot_highs],
            mode="markers", marker=dict(color="#ef4444", size=7,
                                        symbol="triangle-down"),
            name="1h pivot H", yaxis="y",
            hovertemplate="1h pivot H: %{y:.4f}<extra></extra>",
            showlegend=False,
        ))
    if pivot_lows:
        fig.add_trace(go.Scatter(
            x=[t for t, _ in pivot_lows],
            y=[p for _, p in pivot_lows],
            mode="markers", marker=dict(color="#22c55e", size=7,
                                        symbol="triangle-up"),
            name="1h pivot L", yaxis="y",
            hovertemplate="1h pivot L: %{y:.4f}<extra></extra>",
            showlegend=False,
        ))

    # VALIDATED pivots — these are the ones that actually trigger trades under
    # pullback_in_regime. Big labeled markers with SELL/BUY tags so they're
    # impossible to miss on the chart.
    if valid_pivot_highs:
        fig.add_trace(go.Scatter(
            x=[t for t, _ in valid_pivot_highs],
            y=[p * 1.003 for _, p in valid_pivot_highs],
            mode="markers+text",
            marker=dict(color="#fb7185", size=18, symbol="star",
                        line=dict(color="#ffffff", width=2)),
            text=["SELL"] * len(valid_pivot_highs),
            textposition="top center",
            textfont=dict(color="#fb7185", size=11, family="Inter, sans-serif"),
            name="SELL (validated H)", yaxis="y",
            hovertemplate="SELL validated pivot H @ %{y:.4f}<extra></extra>",
            showlegend=False,
        ))
    if valid_pivot_lows:
        fig.add_trace(go.Scatter(
            x=[t for t, _ in valid_pivot_lows],
            y=[p * 0.997 for _, p in valid_pivot_lows],
            mode="markers+text",
            marker=dict(color="#34d399", size=18, symbol="star",
                        line=dict(color="#ffffff", width=2)),
            text=["BUY"] * len(valid_pivot_lows),
            textposition="bottom center",
            textfont=dict(color="#34d399", size=11, family="Inter, sans-serif"),
            name="BUY (validated L)", yaxis="y",
            hovertemplate="BUY validated pivot L @ %{y:.4f}<extra></extra>",
            showlegend=False,
        ))

    # ACTUAL TRADE MARKERS from paper_state trade_history — shows where we
    # actually entered & exited in the visible window.
    try:
        state = _load_state() or {}
        history = state.get("trade_history", []) or []
        entries_long = []; entries_short = []; exits_win = []; exits_loss = []
        for t in history:
            if t.get("symbol") != symbol: continue
            # we only log exits in trade_history — infer entry from entry_bar_ts
            # which isn't in history. For now, mark EXITS on the chart.
            ts_raw = t.get("timestamp") or t.get("ts")
            if ts_raw is None: continue
            try:
                tt = pd.Timestamp(ts_raw)
                if tt.tz is None: tt = tt.tz_localize("UTC")
            except Exception:
                continue
            if tt < df["timestamp"].iloc[0] or tt > df["timestamp"].iloc[-1]:
                continue
            pnl = float(t.get("pnl") or 0)
            px = float(t.get("price") or 0)
            target = exits_win if pnl >= 0 else exits_loss
            target.append((tt, px, pnl))
        if exits_win:
            fig.add_trace(go.Scatter(
                x=[t for t, _, _ in exits_win],
                y=[p for _, p, _ in exits_win],
                mode="markers", marker=dict(color="#22c55e", size=14, symbol="x",
                                            line=dict(color="#ffffff", width=2)),
                name="exit +$", yaxis="y",
                hovertext=[f"EXIT +${pnl:.2f}" for _, _, pnl in exits_win],
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
            ))
        if exits_loss:
            fig.add_trace(go.Scatter(
                x=[t for t, _, _ in exits_loss],
                y=[p for _, p, _ in exits_loss],
                mode="markers", marker=dict(color="#ef4444", size=14, symbol="x",
                                            line=dict(color="#ffffff", width=2)),
                name="exit −$", yaxis="y",
                hovertext=[f"EXIT ${pnl:.2f}" for _, _, pnl in exits_loss],
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
            ))
    except Exception:
        pass

    for y, color in ((70, "#ef4444"), (50, "#6b7280"), (30, "#22c55e")):
        shapes.append(dict(
            type="line", xref="x", yref="y2",
            x0=ts_first, x1=ts_last, y0=y, y1=y,
            line=dict(color=color, width=0.5, dash="dash"),
        ))

    if pos:
        entry = float(pos.get("entry_price", 0))
        sl = float(pos.get("sl", 0))
        side = pos.get("side", "")
        line_data = [
            (entry, "#e5e7eb", "solid", f"ENTRY {entry:.4f} ({side.upper()})"),
        ]
        if sl:
            line_data.append((sl, "#ef4444", "dash", f"SL {sl:.4f}"))
        for key, label in (("tp1", "TP1"), ("tp2", "TP2"), ("tp3", "TP3")):
            price = pos.get(key)
            hit = pos.get(f"{key}_hit")
            if price and not hit:
                line_data.append((float(price), "#22c55e", "dot", f"{label} {float(price):.4f}"))
        trail_off = float(pos.get("trail_offset") or 0)
        if trail_off > 0:
            active = bool(pos.get("trail_active"))
            best = float(pos.get("best_price") or entry)
            if active:
                trail_px = best - trail_off if side == "long" else best + trail_off
                line_data.append((trail_px, "#38bdf8", "dashdot",
                                  f"TRAIL {trail_px:.4f} (active)"))
            else:
                arm = entry + trail_off if side == "long" else entry - trail_off
                line_data.append((arm, "#0891b2", "dot",
                                  f"trail arms @ {arm:.4f}"))

        for y, color, dash, label in line_data:
            shapes.append(dict(
                type="line", xref="x", yref="y",
                x0=ts_first, x1=ts_last, y0=y, y1=y,
                line=dict(color=color, width=1.2, dash=dash),
            ))
            annotations.append(dict(
                xref="paper", yref="y", x=1.0, y=y, xanchor="left",
                text=label, showarrow=False,
                font=dict(color=color, size=10),
            ))

    last = df.iloc[-1]
    # Current regime state for header
    regime_now = "?"
    regime_color = "#9ca3af"
    if regime_segs:
        regime_now = regime_segs[-1][2]
        regime_color = {"up": "#4ade80", "dn": "#f87171",
                        "neutral": "#6b7280"}.get(regime_now, "#9ca3af")
    filter_tag = f"{filter_variant}{'·4h' if require_4h else ''}"
    title = (
        f"<b>{symbol}</b> {interval}  ·  ${last['close']:.4f}  ·  "
        f"RSI {last['rsi']:.1f}  ·  ATR {last['atr']:.4f}  ·  "
        f"<span style='color:{regime_color}'>{filter_tag}={regime_now}</span>"
    )
    if pos:
        entry = float(pos["entry_price"])
        px = float(last["close"])
        pct = (px - entry) / entry * 100.0 * (1 if pos["side"] == "long" else -1)
        color = "#4ade80" if pct >= 0 else "#f87171"
        title += f"  ·  <span style='color:{color}'>{pos['side'].upper()} {pct:+.2f}%</span>"

    fig.update_layout(
        title=dict(text=title, x=0.01, xanchor="left",
                   font=dict(color="#e5e7eb", size=13)),
        paper_bgcolor="#0f1115",
        plot_bgcolor="#0f1115",
        font=dict(color="#9ca3af", size=10),
        margin=dict(l=40, r=80, t=38, b=28),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#111827", bordercolor="#374151",
                        font=dict(color="#e5e7eb", size=11)),
        dragmode="pan",
        showlegend=False,
        # uirevision must be constant so Plotly.react preserves user zoom/pan
        # across auto-refresh. Tie it to the symbol — a symbol switch still resets.
        uirevision=symbol,
        transition=dict(duration=0),
        xaxis=dict(
            rangeslider=dict(visible=False),
            gridcolor="#161c27", griddash="dot",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikecolor="#4b5563", spikethickness=1, spikedash="dot",
            # Initial zoom shows only the most recent INITIAL_VIEW_BARS;
            # the full CHART_BARS of history is still in the trace, so the
            # user can scroll-wheel out / pan-left to reveal older data.
            range=[ts[-min(INITIAL_VIEW_BARS, len(ts))], ts[-1]],
            autorange=False,
            fixedrange=False,
            # Visible axis line + slightly wider tick area so the drag-to-zoom
            # hit zone is easy to grab (like a normal trading chart).
            showline=True, linecolor="#374151",
            ticks="outside", ticklen=5, tickcolor="#374151",
        ),
        yaxis=dict(
            domain=[0.28, 1.0], gridcolor="#161c27", griddash="dot",
            side="right",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikecolor="#4b5563", spikethickness=1, spikedash="dot",
            fixedrange=False,
            autorange=True,
            showline=True, linecolor="#374151",
            ticks="outside", ticklen=5, tickcolor="#374151",
        ),
        yaxis2=dict(
            domain=[0.0, 0.22], gridcolor="#161c27", griddash="dot",
            side="right",
            # Let the user rescale RSI by dragging too — previously locked 0..100.
            fixedrange=False,
            autorange=False, range=[0, 100],
            title=dict(text="RSI", font=dict(size=9)),
            showline=True, linecolor="#374151",
            ticks="outside", ticklen=4, tickcolor="#374151",
        ),
        shapes=shapes,
        annotations=annotations,
    )
    # Use plotly's to_plotly_json so numpy → native types cleanly.
    return json.loads(fig.to_json())


# ---------------------------------------------------------------------------
# Live streaming: one HL WebSocket connection fans candle updates out to every
# browser SSE subscriber.
#   HL WS  →  _ws_consumer (asyncio thread)  →  _live_bars[symbol]
#                                           →  each listener queue
#   /stream (Flask SSE)  →  pops from subscriber queue, yields text/event-stream
# ---------------------------------------------------------------------------

_live_bars: dict[str, dict] = {}
_listeners: set[queue.Queue] = set()
_listeners_lock = threading.Lock()
_ws_ready = threading.Event()

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


def _broadcast(event: dict) -> None:
    payload = json.dumps(event, separators=(",", ":"))
    dead: list[queue.Queue] = []
    with _listeners_lock:
        for q in _listeners:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _listeners.discard(q)


async def _ws_consumer_async(symbols: list[str]) -> None:
    """Single long-lived HL WS connection subscribing to 5m candles for every
    configured symbol. Reconnects with backoff on any failure."""
    import websockets

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(HL_WS_URL, ping_interval=30, ping_timeout=20) as ws:
                for sym in symbols:
                    sub = {
                        "method": "subscribe",
                        "subscription": {"type": "candle", "coin": sym, "interval": "5m"},
                    }
                    await ws.send(json.dumps(sub))
                _ws_ready.set()
                backoff = 1.0
                app.logger.info(f"HL WS connected, subscribed {len(symbols)} symbols")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("channel") != "candle":
                        continue
                    d = msg.get("data") or {}
                    sym = d.get("s")
                    if not sym:
                        continue
                    bar = {
                        "t": int(d["t"]),
                        "T": int(d.get("T", 0)),
                        "o": float(d["o"]),
                        "h": float(d["h"]),
                        "l": float(d["l"]),
                        "c": float(d["c"]),
                        "v": float(d.get("v", 0)),
                    }
                    _live_bars[sym] = bar
                    _broadcast({"symbol": sym, "bar": bar})
        except Exception as exc:
            _ws_ready.clear()
            app.logger.warning(f"HL WS dropped: {exc}; reconnect in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _start_ws_thread() -> None:
    config = load_config()
    symbols = list(config.instruments.keys())

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_ws_consumer_async(symbols))

    threading.Thread(target=_runner, daemon=True, name="hl-ws-consumer").start()


@app.route("/api/live/<path:symbol>")
def api_live(symbol: str):
    bar = _live_bars.get(symbol)
    if not bar:
        return jsonify({"ok": False, "symbol": symbol}), 404
    return jsonify({"ok": True, "symbol": symbol, "bar": bar, "ts": time.time()})


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=200)
    with _listeners_lock:
        _listeners.add(q)

    def gen():
        # Hydrate the subscriber with current state so it paints immediately.
        yield "retry: 3000\n\n"
        for sym, bar in list(_live_bars.items()):
            hello = json.dumps({"symbol": sym, "bar": bar}, separators=(",", ":"))
            yield f"data: {hello}\n\n"
        try:
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _listeners_lock:
                _listeners.discard(q)

    return app.response_class(
        gen(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/")
def index():
    config = load_config()
    symbols = list(config.instruments.keys())
    open_syms = set(_open_symbols())
    ordered = sorted(symbols, key=lambda s: (s not in open_syms, s))
    return _render_index_html(ordered, open_syms)


@app.route("/api/fig/<path:symbol>")
def api_fig(symbol: str):
    fig = _build_plotly_figure(symbol)
    if fig is None:
        return jsonify({"ok": False, "error": "no candles"}), 404
    return jsonify({"ok": True, "symbol": symbol, "fig": fig, "ts": time.time()})


def _build_chart_data(symbol: str, interval: str = "5m", bars: int = CHART_BARS) -> dict | None:
    """Flat JSON payload for the lightweight-charts client — no figure-level
    metadata, just the raw series arrays the chart needs."""
    import pandas as pd
    raw = fetch_candles(symbol, interval, bars)
    if raw.empty:
        return None
    df = add_features(raw).tail(bars).reset_index(drop=True)

    # Unix seconds (lightweight-charts expects integer seconds for intraday).
    ts_s = (df["timestamp"].astype("int64") // 10**9).tolist()

    # 1h regime direction + pivots + BOS trigger levels
    filter_variant, require_4h = _active_filter_for(symbol)
    filter_dir = None
    pivot_highs_out: list[dict] = []
    pivot_lows_out: list[dict] = []
    valid_pivot_highs_out: list[dict] = []
    valid_pivot_lows_out: list[dict] = []
    regime_per_bar: list[dict] = []
    bos_long_level: float | None = None   # most recent confirmed pivot high — bullish BOS above this
    bos_short_level: float | None = None  # most recent confirmed pivot low — bearish BOS below this
    try:
        raw_1h = fetch_candles(symbol, "1h", 200)
        if not raw_1h.empty:
            df_1h = add_features(raw_1h)
            up_arr, dn_arr = _regime_arrays(filter_variant, df, df_1h)
            if len(up_arr):
                i = len(up_arr) - 1
                if up_arr[i] and not dn_arr[i]:
                    filter_dir = "up"
                elif dn_arr[i] and not up_arr[i]:
                    filter_dir = "dn"
                else:
                    filter_dir = "neutral"
            # Per-bar regime for the ribbon histogram
            for j, t in enumerate(ts_s):
                if up_arr[j] and not dn_arr[j]:
                    state = "up"
                elif dn_arr[j] and not up_arr[j]:
                    state = "dn"
                else:
                    state = "neutral"
                regime_per_bar.append({"time": t, "state": state})
            p_h, p_l = _pivots_on_1h(df_1h, lookback=3)
            if p_h:
                bos_long_level = float(p_h[-1][1])
            if p_l:
                bos_short_level = float(p_l[-1][1])
            t0 = df["timestamp"].iloc[0]
            t1 = df["timestamp"].iloc[-1]
            for t, p in p_h:
                if t0 <= t <= t1:
                    pivot_highs_out.append({"time": int(pd.Timestamp(t).timestamp()), "price": float(p)})
            for t, p in p_l:
                if t0 <= t <= t1:
                    pivot_lows_out.append({"time": int(pd.Timestamp(t).timestamp()), "price": float(p)})

            # VALIDATED pivots — what pullback_in_regime actually trades on.
            try:
                from core.quant_filters import combined_pivots_1h
                vh, vl = combined_pivots_1h(df_1h, fractal_lookback=3,
                                             smoothed_lookback=2, atr_min_move=1.0,
                                             vol_spike=1.3, rsi_extreme=(35.0, 65.0))
                for confirm_ts, _, lvl, _ in vh:
                    tt = pd.Timestamp(confirm_ts, unit="ns", tz="UTC")
                    if t0 <= tt <= t1:
                        valid_pivot_highs_out.append({
                            "time": int(tt.timestamp()), "price": float(lvl)
                        })
                for confirm_ts, _, lvl, _ in vl:
                    tt = pd.Timestamp(confirm_ts, unit="ns", tz="UTC")
                    if t0 <= tt <= t1:
                        valid_pivot_lows_out.append({
                            "time": int(tt.timestamp()), "price": float(lvl)
                        })
            except Exception as exc:
                app.logger.debug(f"validated pivots {symbol}: {exc}")
    except Exception as exc:
        app.logger.warning(f"regime/pivots {symbol}: {exc}")

    pos_raw = (_load_state().get("positions") or {}).get(symbol)
    pos_out = None
    if pos_raw:
        pos_out = {
            "entry": float(pos_raw.get("entry_price", 0)),
            "sl": float(pos_raw.get("sl", 0)),
            "side": pos_raw.get("side", ""),
            "tp1": float(pos_raw["tp1"]) if pos_raw.get("tp1") else None,
            "tp2": float(pos_raw["tp2"]) if pos_raw.get("tp2") else None,
            "tp3": float(pos_raw["tp3"]) if pos_raw.get("tp3") else None,
            "tp1_hit": bool(pos_raw.get("tp1_hit", False)),
            "tp2_hit": bool(pos_raw.get("tp2_hit", False)),
            "tp3_hit": bool(pos_raw.get("tp3_hit", False)),
            "trail_offset": float(pos_raw.get("trail_offset") or 0),
            "trail_active": bool(pos_raw.get("trail_active", False)),
            "best_price": float(pos_raw.get("best_price") or 0),
        }

    candles = [
        {"time": t, "open": float(o), "high": float(h), "low": float(l), "close": float(c)}
        for t, o, h, l, c in zip(ts_s, df["open"], df["high"], df["low"], df["close"])
    ]

    def line(col: str) -> list[dict]:
        vals = df[col].tolist()
        return [{"time": t, "value": float(v)} for t, v in zip(ts_s, vals) if v == v]

    last = df.iloc[-1]
    n_view = min(INITIAL_VIEW_BARS, len(ts_s))

    return {
        "ok": True,
        "symbol": symbol,
        "candles": candles,
        "ema9": line("ema_9"),
        "ema21": line("ema_21"),
        "ema50": line("ema_50"),
        "bb_upper": line("bb_upper"),
        "bb_lower": line("bb_lower"),
        "rsi": line("rsi"),
        "position": pos_out,
        "pivots": {"highs": pivot_highs_out, "lows": pivot_lows_out},
        "valid_pivots": {"highs": valid_pivot_highs_out, "lows": valid_pivot_lows_out},
        "regime": regime_per_bar,
        "bos": {"long": bos_long_level, "short": bos_short_level},
        "meta": {
            "last_price": float(last["close"]),
            "rsi": float(last["rsi"]) if last["rsi"] == last["rsi"] else None,
            "atr": float(last["atr"]) if last["atr"] == last["atr"] else None,
            "filter_variant": filter_variant,
            "filter_dir": filter_dir,
            "require_4h": bool(require_4h),
        },
        "initial_view": {"from": ts_s[-n_view], "to": ts_s[-1]},
        "ts": time.time(),
    }


@app.route("/api/data/<path:symbol>")
def api_data(symbol: str):
    data = _build_chart_data(symbol)
    if data is None:
        return jsonify({"ok": False, "symbol": symbol, "error": "no candles"}), 404
    return jsonify(data)


@app.route("/img/<path:filename>")
def serve_img(filename: str):
    if ".." in filename or filename.startswith("/"):
        abort(400)
    return send_from_directory(CHARTS_DIR, filename, max_age=0)


@app.route("/api/render/<path:symbol>")
def api_render_png(symbol: str):
    ok = _render_png(symbol)
    return jsonify({"ok": ok, "symbol": symbol, "ts": time.time()})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "charts_dir": str(CHARTS_DIR)})


@app.route("/favicon.ico")
def favicon():
    # 1×1 transparent PNG served with correct MIME — kills the dev-console 404.
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    return app.response_class(png, mimetype="image/png",
                              headers={"Cache-Control": "public, max-age=86400"})


def _render_index_html(symbols: list[str], open_syms: set[str]) -> str:
    cards = []
    for sym in symbols:
        safe = sym.replace(":", "_").replace("/", "_")
        is_open = sym in open_syms
        badge = (
            '<span class="badge open">● OPEN</span>' if is_open
            else '<span class="badge idle">idle</span>'
        )
        cards.append(f"""
        <div class="card{' open' if is_open else ''}"
             data-symbol="{sym}" data-safe="{safe}"
             data-open="{'1' if is_open else '0'}">
          <div class="hdr">
            <span class="sym">{sym}</span>
            {badge}
            <span class="info" id="info-{safe}">–</span>
            <span class="age" id="age-{safe}">–</span>
            <button class="btn refresh" onclick="refreshOne(this)" title="refresh">↻</button>
            <button class="btn expand" onclick="toggleFullscreen(this)" title="fullscreen">⛶</button>
          </div>
          <div class="panes" id="panes-{safe}">
            <div class="price-pane" id="price-{safe}"></div>
            <div class="rsi-pane" id="rsi-{safe}"></div>
          </div>
        </div>""")

    refresh_ms_open = 30_000
    refresh_ms_idle = 300_000
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Swing Bot — Charts</title>
<script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ margin:0; padding:14px; background:#0a0b0f; color:#e5e7eb;
          font-family: ui-sans-serif, -apple-system, system-ui, "Segoe UI", sans-serif;
          -webkit-font-smoothing: antialiased; }}
  h1 {{ margin:0 0 2px 0; font-size:17px; font-weight:600; letter-spacing:-0.01em; }}
  .meta {{ color:#6b7280; font-size:11px; margin-bottom:10px; }}
  .controls {{ margin-bottom:10px; display:flex; gap:8px; align-items:center; }}
  .controls button {{ background:#1f2937; border:0; color:#e5e7eb;
                      padding:6px 12px; border-radius:5px; font-size:12px;
                      cursor:pointer; transition: background 120ms ease; }}
  .controls button:hover {{ background:#374151; }}
  .controls .note {{ color:#6b7280; font-size:11px; margin-left:auto; }}

  .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(560px, 1fr));
           gap:12px; }}
  .card {{ background:#0f1115; border:1px solid #1f2937; border-radius:8px;
           padding:6px 8px 8px 8px; display:flex; flex-direction:column;
           transition: border-color 160ms ease, box-shadow 160ms ease; }}
  .card.open {{ border-color:#14532d; }}
  .card.open:hover {{ border-color:#16a34a; }}
  .card:hover {{ box-shadow: 0 0 0 1px #263042; }}

  .hdr {{ display:flex; align-items:center; gap:10px; margin-bottom:4px;
          font-size:13px; user-select:none; }}
  .sym {{ font-weight:600; letter-spacing:0.01em; }}
  .badge {{ font-size:10px; padding:2px 6px; border-radius:4px;
            background:#1f2937; color:#9ca3af;
            text-transform: uppercase; letter-spacing:0.05em; }}
  .badge.open {{ background:#052e16; color:#4ade80; }}
  .info {{ color:#d1d5db; font-size:11px; font-variant-numeric: tabular-nums; }}
  .age {{ color:#6b7280; font-size:11px; font-variant-numeric: tabular-nums;
           margin-left:auto; }}

  .btn {{ background:transparent; border:1px solid #1f2937; color:#9ca3af;
          width:24px; height:24px; border-radius:4px; font-size:13px;
          cursor:pointer; display:inline-flex; align-items:center;
          justify-content:center; line-height:1;
          transition: background 120ms ease, border-color 120ms ease, color 120ms ease; }}
  .btn:hover {{ background:#1f2937; border-color:#374151; color:#e5e7eb; }}
  .btn.busy {{ opacity:0.4; cursor:wait; }}

  .panes {{ display:flex; flex-direction:column;
            height: 420px; gap: 1px; background: #0f1115; }}
  .price-pane {{ flex: 1 1 auto; min-height: 0; }}
  .rsi-pane {{ flex: 0 0 90px; min-height: 0; }}

  /* Fullscreen mode */
  .card.fullscreen {{
    position: fixed; inset: 12px;
    z-index: 50;
    padding: 10px 12px 12px 12px;
    background:#0b0d12;
    border-color:#263042;
  }}
  .card.fullscreen .panes {{ flex: 1 1 auto; height: auto; }}
  .card.fullscreen .rsi-pane {{ flex: 0 0 150px; }}
  body.has-fullscreen .grid > .card:not(.fullscreen) {{ visibility: hidden; }}
  body.has-fullscreen {{ overflow:hidden; }}
</style>
</head>
<body>
  <h1>Swing Bot — Live Charts</h1>
  <div class="meta">5m candles · EMA9/21/50 · BB bands · RSI · drag price / time axes to rescale · scroll to zoom</div>
  <div class="controls">
    <button onclick="refreshAll()">refresh all</button>
    <span id="status" class="note">open positions auto 30s · idle 5m · live ticks via WS</span>
  </div>
  <div class="grid">
    {"".join(cards)}
  </div>
<script>
  const OPEN_MS = {refresh_ms_open};
  const IDLE_MS = {refresh_ms_idle};
  const lastFetchedAt = {{}};
  const lastWsBar = {{}};
  const charts = {{}};  // symbol -> {{ priceChart, rsiChart, candle, ema9, ema21, ema50, bbUp, bbLo, rsi, priceLines, resizeObs, syncing }}
  const LWC = LightweightCharts;

  const CHART_COLORS = {{
    up: '#16a34a',
    down: '#dc2626',
    grid: '#161c27',
    border: '#1f2937',
    text: '#9ca3af',
    bg: '#0f1115',
  }};

  function makeChart(el, opts) {{
    return LWC.createChart(el, Object.assign({{
      layout: {{ background: {{ type: 'solid', color: CHART_COLORS.bg }},
                 textColor: CHART_COLORS.text, fontSize: 11,
                 fontFamily: 'ui-sans-serif, system-ui, sans-serif' }},
      grid: {{ vertLines: {{ color: CHART_COLORS.grid }},
              horzLines: {{ color: CHART_COLORS.grid }} }},
      crosshair: {{
        mode: 0,  // normal (follows cursor)
        vertLine: {{ color: '#6b7280', style: 2, width: 1, labelBackgroundColor: '#1f2937' }},
        horzLine: {{ color: '#6b7280', style: 2, width: 1, labelBackgroundColor: '#1f2937' }},
      }},
      rightPriceScale: {{ borderColor: CHART_COLORS.border, scaleMargins: {{ top: 0.08, bottom: 0.08 }} }},
      handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true }},
      handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
    }}, opts));
  }}

  function createCardCharts(card) {{
    const sym = card.dataset.symbol;
    const safe = card.dataset.safe;
    const priceEl = document.getElementById('price-' + safe);
    const rsiEl = document.getElementById('rsi-' + safe);

    const priceChart = makeChart(priceEl, {{
      timeScale: {{ borderColor: CHART_COLORS.border, timeVisible: true,
                    secondsVisible: false, visible: false, rightOffset: 4 }},
    }});
    const rsiChart = makeChart(rsiEl, {{
      timeScale: {{ borderColor: CHART_COLORS.border, timeVisible: true,
                    secondsVisible: false, rightOffset: 4 }},
      rightPriceScale: {{ borderColor: CHART_COLORS.border,
                          scaleMargins: {{ top: 0.1, bottom: 0.1 }} }},
    }});

    const candle = priceChart.addCandlestickSeries({{
      upColor: CHART_COLORS.up, downColor: CHART_COLORS.down,
      borderUpColor: CHART_COLORS.up, borderDownColor: CHART_COLORS.down,
      wickUpColor: CHART_COLORS.up, wickDownColor: CHART_COLORS.down,
      priceLineVisible: true, priceLineWidth: 1, priceLineStyle: 2, priceLineColor: '#6b7280',
    }});
    const lineOpts = (color) => ({{
      color: color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false,
    }});
    const bandOpts = {{ color: '#6b7280', lineWidth: 1, lineStyle: 2,
                        priceLineVisible: false, lastValueVisible: false,
                        crosshairMarkerVisible: false }};

    const ema9 = priceChart.addLineSeries(lineOpts('#fbbf24'));
    const ema21 = priceChart.addLineSeries(lineOpts('#60a5fa'));
    const ema50 = priceChart.addLineSeries(lineOpts('#f472b6'));
    const bbUp = priceChart.addLineSeries(bandOpts);
    const bbLo = priceChart.addLineSeries(bandOpts);

    // Regime ribbon — thin histogram pinned to the bottom ~4% of the price
    // pane, on its own price scale so it never interferes with Y zoom.
    const regime = priceChart.addHistogramSeries({{
      priceScaleId: 'regime',
      priceLineVisible: false,
      lastValueVisible: false,
      base: 0,
    }});
    priceChart.priceScale('regime').applyOptions({{
      scaleMargins: {{ top: 0.96, bottom: 0.0 }},
      visible: false,
    }});

    const rsi = rsiChart.addLineSeries({{
      color: '#a78bfa', lineWidth: 1,
      priceLineVisible: false, lastValueVisible: true,
    }});
    rsi.createPriceLine({{ price: 70, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }});
    rsi.createPriceLine({{ price: 50, color: '#6b7280', lineWidth: 1, lineStyle: 0, axisLabelVisible: false }});
    rsi.createPriceLine({{ price: 30, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }});

    const state = {{ priceChart, rsiChart, candle, ema9, ema21, ema50, bbUp, bbLo, regime, rsi,
                     priceLines: [], bosLines: [], syncing: false, resizeObs: null }};

    // Sync the two time scales so panning/zooming one mirrors the other.
    priceChart.timeScale().subscribeVisibleLogicalRangeChange(r => {{
      if (state.syncing || !r) return;
      state.syncing = true;
      rsiChart.timeScale().setVisibleLogicalRange(r);
      state.syncing = false;
    }});
    rsiChart.timeScale().subscribeVisibleLogicalRangeChange(r => {{
      if (state.syncing || !r) return;
      state.syncing = true;
      priceChart.timeScale().setVisibleLogicalRange(r);
      state.syncing = false;
    }});

    // Fit to container + keep responsive.
    const applySize = () => {{
      const w1 = priceEl.clientWidth, h1 = priceEl.clientHeight;
      const w2 = rsiEl.clientWidth, h2 = rsiEl.clientHeight;
      if (w1 > 0 && h1 > 0) priceChart.applyOptions({{ width: w1, height: h1 }});
      if (w2 > 0 && h2 > 0) rsiChart.applyOptions({{ width: w2, height: h2 }});
    }};
    applySize();
    const ro = new ResizeObserver(applySize);
    ro.observe(priceEl);
    ro.observe(rsiEl);
    state.resizeObs = ro;

    charts[sym] = state;
  }}

  function updateHeader(sym, meta, pos) {{
    const safe = document.querySelector('.card[data-symbol="' + CSS.escape(sym) + '"]').dataset.safe;
    const el = document.getElementById('info-' + safe);
    if (!el) return;
    let txt = '$' + (meta.last_price != null ? meta.last_price.toLocaleString(undefined, {{maximumFractionDigits: 4}}) : '–');
    if (meta.rsi != null) txt += ' · RSI ' + meta.rsi.toFixed(1);
    if (meta.filter_variant && meta.filter_dir) {{
      const dcolor = meta.filter_dir === 'up' ? '#4ade80'
                   : meta.filter_dir === 'dn' ? '#f87171' : '#9ca3af';
      const suffix = meta.require_4h ? '·4h' : '';
      txt += ' · <span style="color:' + dcolor + '">' + meta.filter_variant + suffix + '=' + meta.filter_dir + '</span>';
    }}
    if (pos) {{
      const pct = (meta.last_price - pos.entry) / pos.entry * 100 * (pos.side === 'long' ? 1 : -1);
      const col = pct >= 0 ? '#4ade80' : '#f87171';
      txt += ' · <span style="color:' + col + '">' + pos.side.toUpperCase() + ' ' + pct.toFixed(2) + '%</span>';
    }}
    el.innerHTML = txt;
  }}

  async function loadSymbol(card, first = false) {{
    const sym = card.dataset.symbol;
    try {{
      const r = await fetch('/api/data/' + encodeURIComponent(sym));
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'fetch failed');
      lastFetchedAt[sym] = data.ts;

      const ch = charts[sym];
      if (!ch) return;

      // If we have a newer WS bar cached, merge it into REST data so we render
      // once with the live-truth candle, not the stale REST one.
      const candles = data.candles.slice();
      const bar = lastWsBar[sym];
      if (bar && candles.length) {{
        const tLast = candles[candles.length - 1].time;
        const bt = Math.floor(bar.t / 1000);
        if (bt === tLast) {{
          candles[candles.length - 1] = {{ time: bt, open: bar.o, high: bar.h, low: bar.l, close: bar.c }};
        }} else if (bt > tLast) {{
          candles.push({{ time: bt, open: bar.o, high: bar.h, low: bar.l, close: bar.c }});
        }}
      }}

      ch.candle.setData(candles);
      ch.ema9.setData(data.ema9);
      ch.ema21.setData(data.ema21);
      ch.ema50.setData(data.ema50);
      ch.bbUp.setData(data.bb_upper);
      ch.bbLo.setData(data.bb_lower);
      ch.rsi.setData(data.rsi);

      // Regime ribbon (green=up, red=dn, grey=neutral) — bottom of price pane
      const REGIME_COLOR = {{ up: '#16a34a', dn: '#dc2626', neutral: '#4b5563' }};
      const regimeData = (data.regime || []).map(r => ({{
        time: r.time, value: 1, color: REGIME_COLOR[r.state] || '#4b5563',
      }}));
      ch.regime.setData(regimeData);

      // Refresh price lines for position + BOS trigger levels
      ch.priceLines.forEach(pl => ch.candle.removePriceLine(pl));
      ch.priceLines = [];

      if (data.position) {{
        const p = data.position;
        ch.priceLines.push(ch.candle.createPriceLine({{
          price: p.entry, color: '#e5e7eb', lineWidth: 2, lineStyle: 0,
          axisLabelVisible: true, title: 'ENTRY ' + (p.side || '').toUpperCase(),
        }}));
        if (p.sl) ch.priceLines.push(ch.candle.createPriceLine({{
          price: p.sl, color: '#ef4444', lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: 'SL',
        }}));
        ['tp1', 'tp2', 'tp3'].forEach((k, i) => {{
          if (p[k] && !p[k + '_hit']) {{
            ch.priceLines.push(ch.candle.createPriceLine({{
              price: p[k], color: '#22c55e', lineWidth: 1, lineStyle: 1,
              axisLabelVisible: true, title: 'TP' + (i + 1),
            }}));
          }}
        }});
        if (p.trail_offset > 0) {{
          if (p.trail_active && p.best_price) {{
            const trailPx = p.side === 'long'
              ? p.best_price - p.trail_offset
              : p.best_price + p.trail_offset;
            ch.priceLines.push(ch.candle.createPriceLine({{
              price: trailPx, color: '#38bdf8', lineWidth: 1, lineStyle: 3,
              axisLabelVisible: true, title: 'TRAIL',
            }}));
          }} else {{
            const armPx = p.side === 'long'
              ? p.entry + p.trail_offset
              : p.entry - p.trail_offset;
            ch.priceLines.push(ch.candle.createPriceLine({{
              price: armPx, color: '#0891b2', lineWidth: 1, lineStyle: 1,
              axisLabelVisible: true, title: 'trail arm',
            }}));
          }}
        }}
      }}

      // BOS trigger levels — most recent confirmed 1h pivots. Price breaking
      // above BOS-long (or below BOS-short) flips structure on the 1h.
      if (data.bos) {{
        if (data.bos.long != null) {{
          ch.priceLines.push(ch.candle.createPriceLine({{
            price: data.bos.long, color: '#f59e0b', lineWidth: 1, lineStyle: 1,
            axisLabelVisible: true, title: 'BOS↑',
          }}));
        }}
        if (data.bos.short != null) {{
          ch.priceLines.push(ch.candle.createPriceLine({{
            price: data.bos.short, color: '#f59e0b', lineWidth: 1, lineStyle: 1,
            axisLabelVisible: true, title: 'BOS↓',
          }}));
        }}
      }}

      // Pivot markers (1h swing highs / lows inside the visible 5m window)
      const markers = [];
      (data.pivots.highs || []).forEach(p => markers.push({{
        time: p.time, position: 'aboveBar', color: '#ef4444',
        shape: 'arrowDown', text: '',
      }}));
      (data.pivots.lows || []).forEach(p => markers.push({{
        time: p.time, position: 'belowBar', color: '#22c55e',
        shape: 'arrowUp', text: '',
      }}));
      // VALIDATED pivots — these are the ones pullback_in_regime trades on.
      // Render with a big labeled marker so they stand out from the raw
      // fractal arrows above.
      (data.valid_pivots && data.valid_pivots.highs || []).forEach(p => markers.push({{
        time: p.time, position: 'aboveBar', color: '#fb7185',
        shape: 'arrowDown', text: 'SELL', size: 2,
      }}));
      (data.valid_pivots && data.valid_pivots.lows || []).forEach(p => markers.push({{
        time: p.time, position: 'belowBar', color: '#34d399',
        shape: 'arrowUp', text: 'BUY', size: 2,
      }}));
      markers.sort((a, b) => a.time - b.time);
      ch.candle.setMarkers(markers);

      // Set initial view range only on first load — after that let the user's
      // pan/zoom state persist across refreshes (lightweight-charts preserves
      // visible range by default when setData is called with overlapping data).
      if (first && data.initial_view) {{
        ch.priceChart.timeScale().setVisibleRange(data.initial_view);
      }}

      updateHeader(sym, data.meta, data.position);
    }} catch (e) {{
      console.warn('load', sym, e);
    }}
  }}

  function applyLiveBar(sym, bar) {{
    const prev = lastWsBar[sym];
    if (!prev || bar.t >= prev.t) lastWsBar[sym] = bar;
    const ch = charts[sym];
    if (!ch) return;
    ch.candle.update({{
      time: Math.floor(bar.t / 1000),
      open: bar.o, high: bar.h, low: bar.l, close: bar.c,
    }});
    lastFetchedAt[sym] = Date.now() / 1000;
    // Quick header update with the fresh close price
    const card = document.querySelector('.card[data-symbol="' + CSS.escape(sym) + '"]');
    if (card) {{
      const safe = card.dataset.safe;
      const el = document.getElementById('info-' + safe);
      if (el && el.innerHTML) {{
        // Only replace the $price prefix portion; keep the rest.
        el.innerHTML = el.innerHTML.replace(
          /^\\$[\\d.,]+/,
          '$' + bar.c.toLocaleString(undefined, {{maximumFractionDigits: 4}})
        );
      }}
    }}
  }}

  function setupStream() {{
    const es = new EventSource('/stream');
    es.onmessage = (e) => {{
      try {{
        const d = JSON.parse(e.data);
        if (d.symbol && d.bar) applyLiveBar(d.symbol, d.bar);
      }} catch (_) {{}}
    }};
  }}

  async function refreshOne(btn) {{
    const card = btn.closest('.card');
    btn.classList.add('busy');
    await loadSymbol(card, false);
    btn.classList.remove('busy');
    tickAges();
  }}

  async function refreshAll() {{
    const status = document.getElementById('status');
    status.textContent = 'refreshing…';
    for (const c of document.querySelectorAll('.card')) {{
      await loadSymbol(c, false);
    }}
    status.textContent = 'last: ' + new Date().toLocaleTimeString();
    tickAges();
  }}

  function toggleFullscreen(btn) {{
    const card = btn.closest('.card');
    const willBeFull = !card.classList.contains('fullscreen');
    document.querySelectorAll('.card.fullscreen').forEach(c => {{
      if (c !== card) c.classList.remove('fullscreen');
    }});
    card.classList.toggle('fullscreen', willBeFull);
    document.body.classList.toggle('has-fullscreen', willBeFull);
    btn.textContent = willBeFull ? '✕' : '⛶';
    btn.title = willBeFull ? 'close (esc)' : 'fullscreen';
    // Nudge chart sizing after layout changes
    requestAnimationFrame(() => {{
      const sym = card.dataset.symbol;
      const ch = charts[sym];
      if (!ch) return;
      const safe = card.dataset.safe;
      const priceEl = document.getElementById('price-' + safe);
      const rsiEl = document.getElementById('rsi-' + safe);
      ch.priceChart.applyOptions({{ width: priceEl.clientWidth, height: priceEl.clientHeight }});
      ch.rsiChart.applyOptions({{ width: rsiEl.clientWidth, height: rsiEl.clientHeight }});
    }});
  }}

  function exitFullscreen() {{
    const card = document.querySelector('.card.fullscreen');
    if (card) {{
      const btn = card.querySelector('.btn.expand');
      if (btn) toggleFullscreen(btn);
    }}
  }}
  document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape') exitFullscreen();
  }});

  function ageText(ts) {{
    if (!ts) return '–';
    const secs = Math.floor((Date.now() / 1000) - ts);
    if (secs < 60) return secs + 's ago';
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + 'm ago';
    return Math.floor(mins / 60) + 'h ago';
  }}
  function tickAges() {{
    document.querySelectorAll('.card').forEach(card => {{
      const safe = card.dataset.safe;
      const sym = card.dataset.symbol;
      const el = document.getElementById('age-' + safe);
      if (el) el.textContent = ageText(lastFetchedAt[sym]);
    }});
  }}
  setInterval(tickAges, 5000);

  (async () => {{
    const cards = [...document.querySelectorAll('.card')];
    cards.forEach(createCardCharts);
    for (const c of cards) {{
      await loadSymbol(c, true);
    }}
    tickAges();
    setupStream();

    cards.forEach((c, i) => {{
      const ms = c.dataset.open === '1' ? OPEN_MS : IDLE_MS;
      const jitter = i * 1500;
      setTimeout(() => {{
        loadSymbol(c, false).then(tickAges);
        setInterval(() => loadSymbol(c, false).then(tickAges), ms);
      }}, jitter + ms);
    }});
  }})();
</script>
</body>
</html>
"""


def _prewarm_pngs():
    """Pre-render PNGs once on startup for the LLM/Discord path."""
    config = load_config()
    for s in config.instruments.keys():
        _render_png(s, "5m")


def main():
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_prewarm_pngs, daemon=True).start()
    _start_ws_thread()
    port = int(os.environ.get("CHART_PORT", "5080"))
    print(f"Chart server on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
