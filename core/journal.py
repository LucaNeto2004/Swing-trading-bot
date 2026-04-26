"""Daily paper-trading journal writer.

Writes `logs/daily/YYYY-MM-DD.md` at UTC day-roll. The wiki lint workflow
reads these and promotes notable events into concepts / decisions / log.md.
See `docs/wiki/WIKI.md` for the ingest/lint ops.
"""
from __future__ import annotations

import json
import os
from datetime import date as _date, datetime, timezone
from typing import Any


def _iso_utc_date(ts: Any) -> _date | None:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    except Exception:
        return None


def _fmt(n: float | int | None, spec: str = "+,.2f") -> str:
    if n is None or not isinstance(n, (int, float)):
        return ""
    return format(n, spec)


def write_daily_journal(journal_date: _date,
                        paper_state_path: str,
                        out_dir: str) -> str | None:
    """Write `<out_dir>/<journal_date>.md`.

    Returns the output path, or None when there were no trades that date
    (nothing worth journaling — the day rolls silently)."""
    if not os.path.exists(paper_state_path):
        return None
    with open(paper_state_path) as f:
        state = json.load(f)

    history = state.get("trade_history", []) or []
    day_trades = [t for t in history if _iso_utc_date(t.get("timestamp")) == journal_date]
    if not day_trades:
        return None

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{journal_date.isoformat()}.md")

    n = len(day_trades)
    wins = [t for t in day_trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in day_trades if (t.get("pnl") or 0) < 0]
    pnl = sum((t.get("pnl") or 0) for t in day_trades)
    wr = 100 * len(wins) / n if n else 0.0
    best = max(day_trades, key=lambda t: t.get("pnl") or 0)
    worst = min(day_trades, key=lambda t: t.get("pnl") or 0)
    balance = state.get("balance")

    lines: list[str] = []
    lines.append(f"# {journal_date.isoformat()} — Daily Journal")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
    if isinstance(balance, (int, float)):
        lines.append(f"**End-of-day balance:** ${balance:,.2f}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Trades closed: {n} ({len(wins)}W / {len(losses)}L, WR {wr:.1f}%)")
    lines.append(f"- Net P&L: **${_fmt(pnl)}**")
    lines.append(f"- Best: {best.get('symbol')} {str(best.get('side','')).upper()} "
                 f"${_fmt(best.get('pnl') or 0)} ({best.get('exit_reason','')})")
    lines.append(f"- Worst: {worst.get('symbol')} {str(worst.get('side','')).upper()} "
                 f"${_fmt(worst.get('pnl') or 0)} ({worst.get('exit_reason','')})")
    lines.append("")

    by_sym: dict[str, list[dict]] = {}
    for t in day_trades:
        by_sym.setdefault(t.get("symbol", "?"), []).append(t)
    lines.append("## Per-symbol")
    lines.append("")
    lines.append("| Symbol | Trades | P&L | Exits |")
    lines.append("|---|---:|---:|---|")
    for sym in sorted(by_sym):
        ts_ = by_sym[sym]
        s_pnl = sum((t.get("pnl") or 0) for t in ts_)
        exits = ", ".join(sorted({t.get("exit_reason", "?") for t in ts_}))
        lines.append(f"| {sym} | {len(ts_)} | ${_fmt(s_pnl)} | {exits} |")
    lines.append("")

    lines.append("## Trades")
    lines.append("")
    lines.append("| Time (UTC) | Symbol | Side | P&L | R | Held bars | Exit | FE ATR | AE ATR |")
    lines.append("|---|---|---|---:|---:|---:|---|---:|---:|")
    for t in sorted(day_trades, key=lambda x: str(x.get("timestamp") or "")):
        ts = str(t.get("timestamp") or "")[:19].replace("T", " ")
        r = t.get("r")
        fe = t.get("favorable_excursion_atr")
        ae = t.get("adverse_excursion_atr")
        lines.append(
            f"| {ts} | {t.get('symbol','')} | {str(t.get('side','')).upper()} | "
            f"${_fmt(t.get('pnl') or 0)} | {_fmt(r, '.2f')} | "
            f"{t.get('held_bars','')} | {t.get('exit_reason','')} | "
            f"{_fmt(fe, '.2f')} | {_fmt(ae, '.2f')} |"
        )
    lines.append("")

    open_pos = state.get("positions") or {}
    if open_pos:
        lines.append("## Open at end-of-day")
        lines.append("")
        lines.append("| Symbol | Side | Entry | Size | TP1 hit | Bars held |")
        lines.append("|---|---|---:|---:|:---:|---:|")
        for sym, p in open_pos.items():
            lines.append(
                f"| {sym} | {str(p.get('side','')).upper()} | "
                f"{p.get('entry_price','')} | {p.get('size','')} | "
                f"{'Y' if p.get('tp1_hit') else 'N'} | {p.get('bars_held','')} |"
            )
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("_Filled during weekly wiki lint. Leave blank if nothing notable._")
    lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    return out_path
