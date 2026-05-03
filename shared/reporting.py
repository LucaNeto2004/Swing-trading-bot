"""Weekly trade autopsy generator for both bots.

Reads from the bot's `data/paper_state.json` (which already holds full
`trade_history`), optionally merges structured entries from
`logs/trades.jsonl`, and writes a markdown report to `docs/weekly_report.md`.

CLI:
    python -m shared.reporting --bot commodities-bot
    python -m shared.reporting --bot crypto-bot
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRADING_ROOT = Path(__file__).resolve().parent.parent
BOTS = {
    "commodities-bot": TRADING_ROOT / "commodities-bot",
    "crypto-bot": TRADING_ROOT / "crypto-bot",
}

DEFAULT_LOOKBACK_DAYS = 7


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_paper_trades(bot_dir: Path) -> list[dict]:
    path = bot_dir / "data" / "paper_state.json"
    if not path.exists():
        return []
    with open(path) as f:
        state = json.load(f)
    return state.get("trade_history", []) or []


def _load_jsonl_trades(bot_dir: Path) -> list[dict]:
    path = bot_dir / "logs" / "trades.jsonl"
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _filter_window(trades: list[dict], cutoff: datetime) -> list[dict]:
    kept = []
    for t in trades:
        ts = _parse_ts(t.get("timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(t)
    return kept


def _instrument_of(trade: dict) -> str:
    return trade.get("symbol") or trade.get("instrument") or "UNKNOWN"


def _is_close(trade: dict) -> bool:
    """Only 'close' trades carry realised PnL in the paper_state schema."""
    pnl = trade.get("pnl")
    return pnl is not None and isinstance(pnl, (int, float))


def _is_jsonl_close(trade: dict) -> bool:
    """JSONL closes use a different shape: action ∈ {close, stop_hit} + pnl_usd."""
    return trade.get("action") in ("close", "stop_hit") and isinstance(
        trade.get("pnl_usd"), (int, float)
    )


def _summarise_group(closes: list[dict]) -> dict:
    if not closes:
        return {"trades": 0, "win_rate": 0.0, "pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "best": 0.0, "worst": 0.0}
    pnls = [float(t.get("pnl", 0) or 0) for t in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(closes),
        "win_rate": (len(wins) / len(closes)) if closes else 0.0,
        "pnl": sum(pnls),
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
    }


def _drawdown(closes: list[dict]) -> tuple[float, float]:
    """Return (max_drawdown_usd, current_drawdown_usd) from cumulative PnL."""
    if not closes:
        return 0.0, 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in closes:
        cum += float(t.get("pnl", 0) or 0)
        peak = max(peak, cum)
        dd = cum - peak
        max_dd = min(max_dd, dd)
    return max_dd, cum - peak


def _price_gap_analysis(jsonl_trades: list[dict]) -> dict | None:
    """Only meaningful if the jsonl log is populated (has price_gap field)."""
    gaps = [
        float(t.get("price_gap"))
        for t in jsonl_trades
        if isinstance(t.get("price_gap"), (int, float))
    ]
    if not gaps:
        return None
    per_instrument: dict[str, list[float]] = defaultdict(list)
    for t in jsonl_trades:
        if isinstance(t.get("price_gap"), (int, float)):
            per_instrument[_instrument_of(t)].append(float(t["price_gap"]))
    return {
        "count": len(gaps),
        "mean": sum(gaps) / len(gaps),
        "max_abs": max(abs(g) for g in gaps),
        "per_instrument": {k: sum(v) / len(v) for k, v in per_instrument.items()},
    }


def _regime_skip_stats(jsonl_trades: list[dict]) -> dict | None:
    """Count regime states (and exit reasons) from the structured log."""
    if not jsonl_trades:
        return None
    counts = defaultdict(int)
    for t in jsonl_trades:
        for field in ("regime_state", "regime", "exit_reason"):
            v = t.get(field)
            if v:
                counts[f"{field}={v}"] += 1
    return dict(counts) if counts else None


def _slippage_stats(jsonl_trades: list[dict]) -> dict | None:
    """Compute fill_price - signal_price distribution per instrument."""
    gaps_per: dict[str, list[float]] = defaultdict(list)
    for t in jsonl_trades:
        sp = t.get("signal_price")
        fp = t.get("fill_price")
        if isinstance(sp, (int, float)) and isinstance(fp, (int, float)) and sp > 0:
            inst = _instrument_of(t)
            gaps_per[inst].append(float(fp) - float(sp))
    if not gaps_per:
        return None
    out = {}
    all_gaps = []
    for inst, gaps in gaps_per.items():
        out[inst] = {
            "n": len(gaps),
            "mean": sum(gaps) / len(gaps),
            "max_abs": max(abs(g) for g in gaps),
        }
        all_gaps.extend(gaps)
    out["_overall"] = {
        "n": len(all_gaps),
        "mean": sum(all_gaps) / len(all_gaps),
        "max_abs": max(abs(g) for g in all_gaps),
    }
    return out


def generate_report(bot: str, days: int = DEFAULT_LOOKBACK_DAYS) -> tuple[str, str]:
    """Generate markdown + one-line stdout summary for a bot."""
    if bot not in BOTS:
        raise ValueError(f"Unknown bot {bot!r}; valid: {list(BOTS)}")
    bot_dir = BOTS[bot]

    paper_trades = _load_paper_trades(bot_dir)
    jsonl_trades = _load_jsonl_trades(bot_dir)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    paper_window = _filter_window(paper_trades, cutoff)
    jsonl_window = _filter_window(jsonl_trades, cutoff)
    closes = [t for t in paper_window if _is_close(t)]
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in closes)

    # Load balance for equity display
    equity = None
    state_path = bot_dir / "data" / "paper_state.json"
    if state_path.exists():
        with open(state_path) as f:
            equity = float(json.load(f).get("balance", 0) or 0)

    # Per-instrument breakdown
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in closes:
        groups[_instrument_of(t)].append(t)
    per_instrument = {inst: _summarise_group(trades) for inst, trades in groups.items()}

    max_dd, current_dd = _drawdown(closes)
    regime_stats = _regime_skip_stats(jsonl_window)
    gap_stats = _price_gap_analysis(jsonl_window)
    slippage_stats = _slippage_stats(jsonl_window)

    win_rate = (sum(1 for t in closes if float(t.get("pnl", 0) or 0) > 0) / len(closes)) if closes else 0.0

    lines: list[str] = []
    lines.append(f"# {bot} — Weekly Autopsy")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- **Window**: last {days} days (since {cutoff.date().isoformat()})")
    lines.append(f"- **Total closed trades**: {len(closes)}")
    lines.append(f"- **Win rate**: {win_rate*100:.1f}%")
    lines.append(f"- **Total PnL**: ${total_pnl:+.2f}")
    if equity is not None:
        lines.append(f"- **Current paper equity**: ${equity:,.2f}")
    lines.append("")

    lines.append("## Per-Instrument Breakdown")
    if per_instrument:
        lines.append("| Instrument | Trades | Win % | PnL | Avg Win | Avg Loss | PF | Best | Worst |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for inst, s in sorted(per_instrument.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
            pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
            lines.append(
                f"| {inst} | {s['trades']} | {s['win_rate']*100:.1f}% | ${s['pnl']:+.2f} "
                f"| ${s['avg_win']:+.2f} | ${s['avg_loss']:+.2f} | {pf} "
                f"| ${s['best']:+.2f} | ${s['worst']:+.2f} |"
            )
    else:
        lines.append("_No closed trades in this window._")
    lines.append("")

    lines.append("## Regime / Exit Reasons (from structured log)")
    if regime_stats:
        for state, count in sorted(regime_stats.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {state}: {count}")
    else:
        lines.append("_No structured trade events yet — `logs/trades.jsonl` is empty._")
    lines.append("")

    lines.append("## Slippage (signal vs fill)")
    if slippage_stats:
        overall = slippage_stats.get("_overall", {})
        lines.append(f"- Overall samples: {overall.get('n', 0)}, mean ${overall.get('mean', 0):+.4f}, max |gap| ${overall.get('max_abs', 0):.4f}")
        for inst, s in sorted(slippage_stats.items()):
            if inst == "_overall":
                continue
            lines.append(f"  - {inst}: n={s['n']}, mean ${s['mean']:+.4f}, max |gap| ${s['max_abs']:.4f}")
    else:
        lines.append("_No signal_price/fill_price pairs in the log yet (live trades only)._")
    lines.append("")

    lines.append("## GARCH Forecast Accuracy")
    lines.append("_Requires structured logging with `garch_forecast_vol` field. Skipped for now._")
    lines.append("")

    lines.append("## Price Gap Analysis")
    if gap_stats:
        lines.append(f"- Samples: {gap_stats['count']}")
        lines.append(f"- Mean gap: ${gap_stats['mean']:+.4f}")
        lines.append(f"- Max absolute gap: ${gap_stats['max_abs']:.4f}")
        if gap_stats["per_instrument"]:
            lines.append("- By instrument:")
            for inst, mean_gap in gap_stats["per_instrument"].items():
                lines.append(f"  - {inst}: ${mean_gap:+.4f}")
    else:
        lines.append("_Not tracked yet — relevant only once TV→HL webhook routing is live for commodities._")
    lines.append("")

    lines.append("## Drawdown")
    lines.append(f"- Max drawdown in window: ${max_dd:+.2f}")
    lines.append(f"- Current drawdown from peak: ${current_dd:+.2f}")
    lines.append("")

    lines.append("## Recommendations")
    recs = []
    for inst, s in per_instrument.items():
        if s["trades"] < 5:
            recs.append(f"- **{inst}**: only {s['trades']} trades — not enough to judge.")
        elif s["win_rate"] < 0.4:
            recs.append(f"- **{inst}**: win rate {s['win_rate']*100:.1f}% is below target; consider pausing.")
        elif s["profit_factor"] != float("inf") and s["profit_factor"] < 1.0:
            recs.append(f"- **{inst}**: profit factor {s['profit_factor']:.2f} < 1; losing strategy, needs review.")
    if not recs:
        recs.append("- No red flags this week.")
    lines.extend(recs)

    report = "\n".join(lines) + "\n"

    # Save to docs/weekly_report.md
    docs_dir = bot_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "weekly_report.md"
    out.write_text(report)

    one_liner = (
        f"{bot}: {len(closes)} trades, "
        f"{win_rate*100:.0f}% win rate, ${total_pnl:+.2f} PnL "
        f"({max_dd:+.2f} max DD)"
    )
    return str(out), one_liner


def main() -> None:
    p = argparse.ArgumentParser(description="Weekly trade autopsy generator")
    p.add_argument("--bot", choices=list(BOTS), default="commodities-bot")
    p.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = p.parse_args()

    out_path, summary = generate_report(args.bot, days=args.days)
    print(f"Report written → {out_path}")
    print(summary)


if __name__ == "__main__":
    main()
