"""Render a live chart PNG for one or more symbols.

Reuses the bot's own candle fetcher + feature pipeline so the chart reflects
exactly what the strategy sees. If a paper position is open on the symbol,
entry / SL / TP1-3 are overlaid as horizontal lines. Output lands in
``data/charts/<SYMBOL>_<INTERVAL>.png``.

Examples:
    python scripts/snapshot_chart.py HYPE
    python scripts/snapshot_chart.py --open
    python scripts/snapshot_chart.py --all --interval 1h
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config.settings import load_config  # noqa: E402
from core.data import fetch_candles  # noqa: E402
from core.features import add_features  # noqa: E402

CHARTS_DIR = REPO_ROOT / "data" / "charts"
PAPER_STATE = REPO_ROOT / "data" / "paper_state.json"

UP = "#16a34a"
DOWN = "#dc2626"
BG = "#0f1115"
GRID = "#1f2937"
TEXT = "#e5e7eb"
MUTED = "#9ca3af"


def _load_state() -> dict:
    try:
        with open(PAPER_STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _get_position(symbol: str, state: dict) -> dict | None:
    return (state.get("positions") or {}).get(symbol)


def _draw_candles(ax, df) -> None:
    o = df["open"].to_numpy()
    c = df["close"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    x = np.arange(len(df))
    up = c >= o

    ax.vlines(x[up], l[up], h[up], color=UP, lw=0.9)
    ax.vlines(x[~up], l[~up], h[~up], color=DOWN, lw=0.9)

    width = 0.7
    up_bodies = [
        Rectangle((x[i] - width / 2, o[i]), width, max(c[i] - o[i], 1e-9))
        for i in np.where(up)[0]
    ]
    down_bodies = [
        Rectangle((x[i] - width / 2, c[i]), width, max(o[i] - c[i], 1e-9))
        for i in np.where(~up)[0]
    ]
    ax.add_collection(PatchCollection(up_bodies, facecolor=UP, edgecolor=UP))
    ax.add_collection(PatchCollection(down_bodies, facecolor=DOWN, edgecolor=DOWN))


def _style_axes(*axes) -> None:
    for ax in axes:
        ax.set_facecolor(BG)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.tick_params(colors=MUTED)
        ax.yaxis.label.set_color(MUTED)
        ax.grid(color=GRID, alpha=0.35, lw=0.5)


def render(symbol: str, interval: str = "5m", bars: int = 200) -> Path | None:
    raw = fetch_candles(symbol, interval, bars)
    if raw.empty:
        print(f"[skip] {symbol} {interval}: no candles")
        return None
    df = add_features(raw).tail(bars).reset_index(drop=True)

    state = _load_state()
    pos = _get_position(symbol, state)

    fig, (ax_px, ax_rsi) = plt.subplots(
        2, 1, figsize=(14, 8),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor(BG)
    _style_axes(ax_px, ax_rsi)

    _draw_candles(ax_px, df)

    x = np.arange(len(df))
    ax_px.plot(x, df["ema_9"], color="#fbbf24", lw=1.0, label="EMA9", alpha=0.85)
    ax_px.plot(x, df["ema_21"], color="#60a5fa", lw=1.0, label="EMA21", alpha=0.85)
    ax_px.plot(x, df["ema_50"], color="#f472b6", lw=1.0, label="EMA50", alpha=0.85)
    ax_px.plot(x, df["bb_upper"], color="#6b7280", lw=0.7, linestyle="--", alpha=0.55)
    ax_px.plot(x, df["bb_lower"], color="#6b7280", lw=0.7, linestyle="--", alpha=0.55)

    span = [-0.5, len(df) - 0.5]
    if pos:
        entry = float(pos.get("entry_price", 0))
        sl = float(pos.get("sl", 0))
        side = pos.get("side", "")
        ax_px.hlines(entry, *span, color=TEXT, lw=1.5, label=f"ENTRY {entry:.4f}")
        if sl:
            ax_px.hlines(sl, *span, color="#ef4444", lw=1.2, linestyle="--",
                         label=f"SL {sl:.4f}")
        for key, alpha, lbl in (("tp1", 0.9, "TP1"), ("tp2", 0.7, "TP2"), ("tp3", 0.5, "TP3")):
            price = pos.get(key)
            hit = pos.get(f"{key}_hit")
            if price and not hit:
                ax_px.hlines(float(price), *span, color="#22c55e", lw=1.0,
                             linestyle=":", alpha=alpha,
                             label=f"{lbl} {float(price):.4f}")

    last = df.iloc[-1]
    title = (
        f"{symbol} {interval}  |  ${last['close']:.4f}  |  "
        f"RSI {last['rsi']:.1f}  |  ATR {last['atr']:.4f}"
    )
    if pos:
        entry = float(pos["entry_price"])
        px = float(last["close"])
        pct = (px - entry) / entry * 100.0 * (1 if pos["side"] == "long" else -1)
        title += f"  |  {pos['side'].upper()} {entry:.4f} ({pct:+.2f}%)"
    ax_px.set_title(title, color=TEXT, fontsize=12, loc="left", pad=10)
    ax_px.legend(
        loc="upper left", fontsize=8, facecolor="#111827",
        edgecolor=GRID, labelcolor=MUTED, framealpha=0.9,
    )
    ax_px.set_xlim(-1, len(df))

    ax_rsi.plot(x, df["rsi"], color="#a78bfa", lw=1.0)
    ax_rsi.axhline(70, color="#ef4444", lw=0.5, linestyle="--", alpha=0.55)
    ax_rsi.axhline(30, color="#22c55e", lw=0.5, linestyle="--", alpha=0.55)
    ax_rsi.axhline(50, color="#6b7280", lw=0.5, alpha=0.3)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI")

    tick_idx = np.linspace(0, len(df) - 1, 8, dtype=int)
    ax_rsi.set_xticks(tick_idx)
    ax_rsi.set_xticklabels(
        [df["timestamp"].iloc[i].strftime("%m-%d %H:%M") for i in tick_idx],
        rotation=25, ha="right", fontsize=8,
    )

    plt.tight_layout()
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = symbol.replace(":", "_")
    out = CHARTS_DIR / f"{safe}_{interval}.png"
    plt.savefig(out, dpi=110, facecolor=BG)
    plt.close(fig)
    print(f"[ok] {symbol} {interval} → {out}")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("symbol", nargs="?", help="symbol to render (e.g. HYPE, xyz:CL)")
    p.add_argument("--all", action="store_true", help="render every configured symbol")
    p.add_argument("--open", dest="open_only", action="store_true",
                   help="render only symbols with an open paper position")
    p.add_argument("--interval", default="5m", choices=["5m", "1h"])
    p.add_argument("--bars", type=int, default=200)
    args = p.parse_args()

    config = load_config()
    all_symbols = list(config.instruments.keys())

    if args.open_only:
        targets = list((_load_state().get("positions") or {}).keys())
        if not targets:
            print("no open positions")
            return
    elif args.all:
        targets = all_symbols
    elif args.symbol:
        targets = [args.symbol]
    else:
        p.error("pass a symbol, --all, or --open")

    for sym in targets:
        render(sym, args.interval, args.bars)


if __name__ == "__main__":
    main()
