"""Static backtest replay chart — renders BOS entries/exits on 15m candles.

Runs the backtest for a single symbol with the proposed BOS config, then
overlays each trade's entry (green ▲) and exit (red ▼ or green ▼ by outcome)
on the 15m chart. Writes PNG to /tmp/bos_replay_<sym>.png.

Use: python scripts/render_bos_replay.py BTC
     (if no arg, renders all 5 validated swap candidates)
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb


def _cfg_from_deployed(d):
    return cb.Cfg(
        trend_filter=d["trend_filter"], entry_type=d["entry_type"],
        rsi_oversold=float(d["rsi_oversold"]), rsi_overbought=float(d["rsi_overbought"]),
        sl_atr=float(d["sl_atr"]), tp1_atr=float(d["tp1_atr"]),
        tp1_pct=float(d["tp1_pct"]),
        tp2_atr=float(d.get("tp2_atr", 0.0)), tp2_pct=float(d.get("tp2_pct", 0.0)),
        tp3_atr=float(d.get("tp3_atr", 0.0)), tp3_pct=float(d.get("tp3_pct", 0.0)),
        trail_atr=float(d["trail_atr"]), max_hold_bars=int(d["max_hold_bars"]),
        direction=d["direction"], use_1h_filter=bool(d["use_1h_filter"]),
        trend_filter_1h=d.get("trend_filter_1h", "ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement", False)),
    )


# BOS configs per symbol — matches validated results from bos_with_quant_test
BOS_CONFIGS = {
    "BTC":   dict(entry_type="bos_structural", exit_type="bos_structural",
                  tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0),
    "kPEPE": dict(entry_type="bos_structural", exit_type="bos_structural",
                  tp1_atr=0.0, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0),
    "HYPE":  dict(entry_type="bos_structural", exit_type="bos_hybrid",
                  tp1_atr=2.0, tp1_pct=0.3,
                  tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0),
    "SOL":   dict(entry_type="bos_structural", exit_type="bos_hybrid",
                  tp1_atr=2.0, tp1_pct=0.3,
                  tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0),
    "XRP":   dict(entry_type="bos_structural", exit_type="bos_hybrid",
                  tp1_atr=2.0, tp1_pct=0.3,
                  tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                  require_funding_confirm=True),
}


def _rerun_and_collect(arr, base_cfg, overrides, lev):
    """Re-run backtest and return per-event list (entry + exit points)."""
    cfg = replace(base_cfg, **overrides)
    n = len(arr["close"])
    close = arr["close"]; high = arr["high"]; low = arr["low"]
    rsi = arr["rsi"]; atr = arr["atr"]
    e21, e50, e200 = arr["ema_21"], arr["ema_50"], arr["ema_200"]
    slope = arr["ema_50_slope"]
    bbl, bbu = arr["bb_lower"], arr["bb_upper"]
    ts = arr["timestamp"]
    weekday = arr["weekday"]

    if cfg.trend_filter_1h == "structure":
        up_1h, dn_1h = arr["up_struct"], arr["dn_struct"]
    elif cfg.trend_filter_1h == "both_agree":
        up_1h = arr["up_1h"] & arr["up_struct"]
        dn_1h = arr["dn_1h"] & arr["dn_struct"]
    elif cfg.trend_filter_1h == "hma_slope":
        up_1h, dn_1h = arr["up_hma"], arr["dn_hma"]
    elif cfg.trend_filter_1h == "sjm":
        up_1h, dn_1h = arr["up_sjm"], arr["dn_sjm"]
    elif cfg.trend_filter_1h == "kalman":
        up_1h, dn_1h = arr["up_kalman"], arr["dn_kalman"]
    else:
        up_1h, dn_1h = arr["up_1h"], arr["dn_1h"]

    events = []  # (ts, price, kind, side, pnl)
    position = None
    tp1_hit = tp2_hit = tp3_hit = False

    for i in range(52, n):
        a_i = atr[i]; r = rsi[i]
        if a_i <= 0 or a_i != a_i or r != r:
            continue
        price = close[i]; hi = high[i]; lo = low[i]

        if position is not None:
            side = position["side"]; entry = position["entry"]
            bos_exit_hit = False
            opp = arr["last_pivot_l"][i] if side == "long" else arr["last_pivot_h"][i]
            if not np.isnan(opp):
                if side == "long" and close[i] < opp: bos_exit_hit = True
                elif side == "short" and close[i] > opp: bos_exit_hit = True

            tp1 = position.get("tp1")
            if cfg.exit_type == "bos_hybrid" and not tp1_hit and tp1 is not None:
                if (side == "long" and hi >= tp1) or (side == "short" and lo <= tp1):
                    tp1_hit = True
                    events.append({"ts": ts[i], "price": tp1, "kind": "tp1", "side": side, "pnl": 0})

            bars_in = i - position["entry_bar"]
            max_hold = bars_in >= cfg.max_hold_bars
            if bos_exit_hit or max_hold:
                ep = opp if bos_exit_hit else price
                pnl = ((ep - entry) if side == "long" else (entry - ep)) * position["size"]
                events.append({"ts": ts[i], "price": ep, "kind": "exit",
                               "side": side, "pnl": pnl,
                               "reason": "bos" if bos_exit_hit else "max_hold"})
                position = None
                tp1_hit = tp2_hit = tp3_hit = False
                continue

        if position is not None:
            continue
        if not weekday[i]:
            continue

        up_ok = up_1h[i]; dn_ok = dn_1h[i]
        if cfg.require_4h_agreement:
            up_ok = up_ok and arr["up_4h"][i]
            dn_ok = dn_ok and arr["dn_4h"][i]
        if cfg.require_funding_confirm and "funding_extreme" in arr:
            fe = arr["funding_extreme"][i]
            if fe != -1: up_ok = False
            if fe != 1: dn_ok = False
        if cfg.direction == "long_only": dn_ok = False
        elif cfg.direction == "short_only": up_ok = False

        ph = arr["last_pivot_h"][i]; pl = arr["last_pivot_l"][i]
        long_trig = short_trig = False
        if up_ok and not np.isnan(ph) and close[i - 1] <= ph and price > ph:
            long_trig = True
        if dn_ok and not np.isnan(pl) and close[i - 1] >= pl and price < pl:
            short_trig = True

        if long_trig or short_trig:
            side = "long" if long_trig else "short"
            notional = 10000 * 0.15 * lev / 0.15  # leverage already applied
            size = notional / price
            tp1 = price + a_i * cfg.tp1_atr if (side == "long" and cfg.tp1_atr > 0) else (
                  price - a_i * cfg.tp1_atr if (side == "short" and cfg.tp1_atr > 0) else None)
            position = dict(entry=price, side=side, size=size, tp1=tp1, entry_bar=i)
            events.append({"ts": ts[i], "price": price, "kind": "entry", "side": side, "pnl": 0})

    return events


def render(sym: str, outdir: str = "/tmp"):
    deployed = load_all()
    if sym not in deployed:
        print(f"{sym}: no deployed config"); return
    if sym not in BOS_CONFIGS:
        print(f"{sym}: no BOS override defined"); return

    d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
    d1h = cb.add_features(cb.fetch_hl(sym, "1h", 2000))
    d4h = cb.add_features(cb.fetch_hl(sym, "4h", 1000))
    arr = cb.precompute(d15, d1h, d4h)
    if not sym.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)

    # Funding augmentation if the config needs it
    overrides = BOS_CONFIGS[sym]
    if overrides.get("require_funding_confirm") and not sym.startswith("xyz:"):
        _bot_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        _shared = os.path.join(_bot_root, "shared")
        if not os.path.isdir(_shared):
            _shared = os.path.abspath(os.path.join(_bot_root, "..", "shared"))
        sys.path.insert(0, _shared)
        import hl_client
        from core.features import add_funding_features
        t0_ms = int(d15["timestamp"].iloc[0].timestamp() * 1000)
        t1_ms = int(d15["timestamp"].iloc[-1].timestamp() * 1000)
        fund = hl_client.sync_get_funding_history(sym, t0_ms - 86400*1000, t1_ms)
        if not fund.empty:
            enriched = add_funding_features(d15, fund)
            arr["funding_extreme"] = enriched["funding_extreme"].to_numpy()
    base_cfg = _cfg_from_deployed(deployed[sym])
    lev = INSTRUMENTS[sym].hl_max_leverage * 0.15

    events = _rerun_and_collect(arr, base_cfg, overrides, lev)
    entries = [e for e in events if e["kind"] == "entry"]
    exits = [e for e in events if e["kind"] == "exit"]
    tp1s = [e for e in events if e["kind"] == "tp1"]
    total_pnl = sum(e["pnl"] for e in exits)
    winrate = sum(1 for e in exits if e["pnl"] > 0) / len(exits) * 100 if exits else 0

    # Plot the last ~N bars of the series
    PLOT_BARS = min(len(d15), 1500)
    df = d15.tail(PLOT_BARS).reset_index(drop=True)

    fig, (ax, ax_rsi) = plt.subplots(2, 1, figsize=(18, 9), sharex=True,
                                     gridspec_kw={"height_ratios": [3, 1]},
                                     facecolor="#0b0f17")
    ax.set_facecolor("#0b0f17"); ax_rsi.set_facecolor("#0b0f17")

    # Candles (simplified as high-low bar + open-close rectangle)
    for _, row in df.iterrows():
        color = "#16a34a" if row["close"] >= row["open"] else "#dc2626"
        ax.plot([row["timestamp"], row["timestamp"]], [row["low"], row["high"]],
                color=color, linewidth=0.6, alpha=0.85)
        y0, y1 = sorted([row["open"], row["close"]])
        ax.fill_between([row["timestamp"]], y0, y1, color=color, alpha=0.6)

    # EMA21/50
    ax.plot(df["timestamp"], df["ema_21"], color="#60a5fa", linewidth=0.8, alpha=0.8, label="EMA21")
    ax.plot(df["timestamp"], df["ema_50"], color="#f472b6", linewidth=0.8, alpha=0.8, label="EMA50")

    # Pivot levels over time (last_pivot_h/l evolve, draw as step-line)
    step_h = arr["last_pivot_h"][-PLOT_BARS:]
    step_l = arr["last_pivot_l"][-PLOT_BARS:]
    ax.plot(df["timestamp"], step_h, color="#ef4444", linewidth=0.5, alpha=0.35, linestyle="--", label="pivot H (break target)")
    ax.plot(df["timestamp"], step_l, color="#22c55e", linewidth=0.5, alpha=0.35, linestyle="--", label="pivot L (break target)")

    # Entries + exits inside the plot window (normalize tz so compare works)
    def _to_utc(x):
        t = pd.Timestamp(x)
        if t.tzinfo is None: t = t.tz_localize("UTC")
        else: t = t.tz_convert("UTC")
        return t
    t0 = _to_utc(df["timestamp"].iloc[0])
    t1 = _to_utc(df["timestamp"].iloc[-1])
    for e in entries + exits + tp1s:
        e["ts"] = _to_utc(e["ts"])
    plot_entries = [e for e in entries if t0 <= e["ts"] <= t1]
    plot_exits = [e for e in exits if t0 <= e["ts"] <= t1]
    plot_tp1s = [e for e in tp1s if t0 <= e["ts"] <= t1]

    for e in plot_entries:
        marker = "^" if e["side"] == "long" else "v"
        color = "#4ade80" if e["side"] == "long" else "#f87171"
        ax.scatter(e["ts"], e["price"], marker=marker, s=110, color=color,
                   edgecolor="white", linewidth=1.0, zorder=5)
    for e in plot_exits:
        color = "#fbbf24" if e["pnl"] > 0 else "#9ca3af"
        ax.scatter(e["ts"], e["price"], marker="x", s=80, color=color,
                   linewidth=2.0, zorder=5)
    for e in plot_tp1s:
        ax.scatter(e["ts"], e["price"], marker="o", s=35, color="#fde047",
                   edgecolor="#854d0e", linewidth=0.8, zorder=5)

    # Draw lines connecting entry to exit
    exit_iter = iter(plot_exits)
    for ent in plot_entries:
        try:
            ex = next(exit_iter)
            c = "#4ade80" if ex["pnl"] > 0 else "#f87171"
            ax.plot([ent["ts"], ex["ts"]], [ent["price"], ex["price"]],
                    color=c, linewidth=1.2, alpha=0.5, zorder=4)
        except StopIteration:
            break

    # RSI
    ax_rsi.plot(df["timestamp"], df["rsi"], color="#a78bfa", linewidth=0.8)
    ax_rsi.axhline(70, color="#ef4444", linestyle="--", linewidth=0.5, alpha=0.5)
    ax_rsi.axhline(30, color="#22c55e", linestyle="--", linewidth=0.5, alpha=0.5)
    ax_rsi.axhline(50, color="#6b7280", linestyle="--", linewidth=0.5, alpha=0.5)

    # Styling
    cfg_label = ", ".join(f"{k}={v}" for k, v in overrides.items() if k in (
        "entry_type", "exit_type", "require_funding_confirm"))
    ax.set_title(f"{sym} — BOS backtest replay   |   {len(plot_entries)}/{len(entries)} trades shown   "
                 f"|   full-sample $: ${total_pnl:+.0f}   WR: {winrate:.0f}%   "
                 f"|   {cfg_label}",
                 color="white", fontsize=11)
    ax.set_ylabel("price", color="#9ca3af"); ax_rsi.set_ylabel("RSI", color="#9ca3af")
    for a in (ax, ax_rsi):
        a.tick_params(colors="#9ca3af")
        for spine in a.spines.values():
            spine.set_color("#1f2937")
        a.grid(True, alpha=0.15, color="#1f2937")
    ax.legend(loc="upper left", facecolor="#0b0f17", edgecolor="#1f2937",
              labelcolor="#9ca3af", fontsize=8)

    ax_rsi.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax_rsi.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax_rsi.xaxis.get_majorticklabels(), rotation=30, ha="right")

    plt.tight_layout()
    safe = sym.replace(":", "_")
    path = os.path.join(outdir, f"bos_replay_{safe}.png")
    plt.savefig(path, dpi=120, facecolor="#0b0f17")
    plt.close()
    print(f"  {sym}: {len(entries)} entries, ${total_pnl:+.0f} total, "
          f"{winrate:.0f}% WR → {path}")


def main():
    syms = sys.argv[1:] if len(sys.argv) > 1 else list(BOS_CONFIGS.keys())
    for sym in syms:
        try:
            render(sym)
        except Exception as e:
            import traceback
            print(f"{sym} failed: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
