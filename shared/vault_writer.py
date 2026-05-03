"""Obsidian vault writer — drops markdown notes for trades, research runs and incidents.

No-op if VAULT_PATH is unset or the directory does not exist, so both bots can
import this safely even without Obsidian installed.

Public API:
    write_trade_note(trade: dict) -> Optional[Path]
    write_research_note(job_id: str, symbol: str|None, result: dict) -> Optional[Path]
    write_incident_note(kind: str, detail: str, extra: dict|None = None) -> Optional[Path]

All failures are swallowed and logged — the vault writer must never break the
trading loop.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("vault_writer")

DEFAULT_VAULT = Path("/Users/lucaneto/obsidian vault/Trading")


def _vault_root() -> Optional[Path]:
    env = os.environ.get("VAULT_PATH")
    root = Path(env) if env else DEFAULT_VAULT
    if not root.exists():
        return None
    return root


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(text)).strip("-").lower()
    return s or "x"


def _write(folder: str, filename: str, body: str) -> Optional[Path]:
    root = _vault_root()
    if root is None:
        return None
    try:
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        path = target / filename
        path.write_text(body, encoding="utf-8")
        return path
    except Exception as e:
        log.warning("vault write failed: %s", e)
        return None


def _fmt_money(v) -> str:
    try:
        return f"${float(v):+,.2f}"
    except Exception:
        return str(v)


def _front_matter(tags: list[str], **kv) -> str:
    lines = ["---"]
    for k, v in kv.items():
        if v is None:
            continue
        lines.append(f"{k}: {v}")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {t}")
    lines.append("---\n")
    return "\n".join(lines)


def write_trade_note(trade: dict) -> Optional[Path]:
    """Write a note when a position closes. `trade` must contain:
       symbol, side, entry_price, exit_price, size, pnl, exit_reason, strategy.
       Optional: entry_time, exit_time, stop_loss, take_profit, size_usd, balance_after.
    """
    symbol = trade.get("symbol", "UNKNOWN")
    bot = trade.get("bot", "unknown")
    pnl = trade.get("pnl", 0.0) or 0.0
    now = trade.get("exit_time") or datetime.now()
    if isinstance(now, str):
        try:
            now = datetime.fromisoformat(now)
        except Exception:
            now = datetime.now()

    side = trade.get("side", "").replace("close_", "")
    reason = trade.get("exit_reason") or "signal"
    strategy = trade.get("strategy") or "momentum"
    win = pnl > 0

    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    sym_slug = _slug(symbol)
    filename = f"{stamp}_{sym_slug}_{side}.md"

    tags = [
        "trade",
        f"symbol/{sym_slug}",
        f"bot/{bot}",
        f"exit/{_slug(reason)}",
        "win" if win else "loss",
    ]

    front = _front_matter(
        tags,
        symbol=symbol,
        bot=bot,
        side=side,
        strategy=strategy,
        exit_reason=reason,
        pnl=round(float(pnl), 2),
        result="win" if win else "loss",
        closed_at=now.isoformat(timespec="seconds"),
    )

    lines = [
        front,
        f"# {symbol} {side.upper()} — {_fmt_money(pnl)}",
        "",
        f"Closed {now.strftime('%Y-%m-%d %H:%M:%S')} · reason: `{reason}` · strategy: `{strategy}`",
        "",
        "## Fill",
        "| Field | Value |",
        "|---|---|",
        f"| Entry | {trade.get('entry_price', '?')} |",
        f"| Exit | {trade.get('exit_price', '?')} |",
        f"| Size | {trade.get('size', '?')} |",
        f"| Size (USD) | {trade.get('size_usd', '?')} |",
        f"| Stop Loss | {trade.get('stop_loss', '—')} |",
        f"| Take Profit | {trade.get('take_profit', '—')} |",
        f"| P&L | **{_fmt_money(pnl)}** |",
        f"| Balance after | {_fmt_money(trade.get('balance_after', 0))} |",
        "",
        "## Context",
        f"- Strategy: [[strategies/{_slug(strategy)}|{strategy}]]",
        f"- Symbol: [[symbols/{sym_slug}|{symbol}]]",
        "",
        "## Notes",
        "_(free-form observations — why did this work / fail?)_",
        "",
    ]
    return _write("trades", filename, "\n".join(lines))


def write_research_note(job_id: str, symbol: Optional[str], result: dict) -> Optional[Path]:
    """Write a note when an optimizer/research job finishes.
       `result` is the raw job dict from dashboard._jobs[job_id].
    """
    now = datetime.now()
    sym = symbol or "all"
    sym_slug = _slug(sym)
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    bot = result.get("bot", "unknown")
    filename = f"{stamp}_{sym_slug}_{job_id}.md"
    status = result.get("status", "?")

    tags = [
        "research",
        f"symbol/{sym_slug}",
        f"bot/{bot}",
        f"status/{_slug(status)}",
    ]

    front = _front_matter(
        tags,
        symbol=sym,
        bot=bot,
        job_id=job_id,
        status=status,
        started_at=result.get("created_at"),
        finished_at=now.isoformat(timespec="seconds"),
    )

    lines = [
        front,
        f"# Research run — {sym} ({status})",
        "",
        f"Job `{job_id}` · bot `{bot}` · finished {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    pending = result.get("pending_results") or {}
    rows = pending.get("results") or []
    if rows:
        lines.append("## Candidates")
        lines.append("| Strategy | Symbol | Grade | Sharpe | PF | WR% | MaxDD | Trades | Deployable |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r.get('strategy','?')} | {r.get('symbol','?')} "
                f"| {r.get('grade','?')} | {r.get('sharpe','?')} "
                f"| {r.get('profit_factor','?')} | {r.get('win_rate','?')} "
                f"| {r.get('max_drawdown','?')} | {r.get('num_trades','?')} "
                f"| {'✅' if r.get('deployable') else '❌'} |"
            )
        lines.append("")
        for r in rows:
            lines.append(f"### {r.get('strategy','?')} · {r.get('symbol','?')}")
            params = r.get("params") or {}
            if params:
                lines.append("```json")
                import json as _json
                lines.append(_json.dumps(params, indent=2))
                lines.append("```")
            if r.get("deploy_reason"):
                lines.append(f"- Deploy reason: `{r.get('deploy_reason')}`")
            lines.append("")

    err = result.get("error")
    stderr = (result.get("result") or {}).get("stderr")
    if err or stderr:
        lines.append("## Errors")
        if err:
            lines.append(f"- {err}")
        if stderr:
            lines.append("```")
            lines.append(stderr[-800:])
            lines.append("```")
        lines.append("")

    lines.append("## Decision")
    lines.append("_(deployed / rejected / pending — why?)_")
    lines.append("")
    return _write("research", filename, "\n".join(lines))


def write_deploy_note(strategy: str, symbol: str, params: dict, metrics: dict,
                      forced: bool = False, bot: str = "unknown") -> Optional[Path]:
    """Audit trail: every time parameters are deployed to config/deployed/ a
    note lands here. Lets Luca see the full history of param drift over time.
    """
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    sym_slug = _slug(symbol)
    strat_slug = _slug(strategy)
    filename = f"{stamp}_{strat_slug}_{sym_slug}.md"

    grade = (metrics or {}).get("grade", "?")
    tags = [
        "deploy",
        f"strategy/{strat_slug}",
        f"symbol/{sym_slug}",
        f"bot/{bot}",
        f"grade/{_slug(grade)}",
    ]
    if forced:
        tags.append("forced")

    front = _front_matter(
        tags,
        strategy=strategy,
        symbol=symbol,
        bot=bot,
        grade=grade,
        score=(metrics or {}).get("score"),
        sharpe=(metrics or {}).get("sharpe"),
        max_drawdown=(metrics or {}).get("max_drawdown"),
        win_rate=(metrics or {}).get("win_rate"),
        profit_factor=(metrics or {}).get("profit_factor"),
        num_trades=(metrics or {}).get("num_trades"),
        total_return_pct=(metrics or {}).get("total_return_pct"),
        forced=forced,
        deployed_at=now.isoformat(timespec="seconds"),
    )

    lines = [
        front,
        f"# Deploy — {strategy} · {symbol}",
        "",
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} · grade `{grade}` · bot `{bot}`"
        + (" · **forced**" if forced else ""),
        "",
        "## Metrics",
        "| | |",
        "|---|---|",
    ]
    for k, v in (metrics or {}).items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Parameters")
    lines.append("```json")
    import json as _json
    lines.append(_json.dumps(params or {}, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Related")
    lines.append(f"- Strategy: [[strategies/{strat_slug}|{strategy}]]")
    lines.append(f"- Symbol: [[symbols/{sym_slug}|{symbol}]]")
    lines.append("")
    lines.append("## Post-deploy observations")
    lines.append("_(how is the new config performing vs the old? fill in after ~1 week of live data)_")
    lines.append("")
    return _write("deploys", filename, "\n".join(lines))


def write_regime_snapshot(symbol: str, profile: dict, bot: str = "unknown") -> Optional[Path]:
    """Write a daily regime snapshot for a symbol — called by
    scripts/daily_adaptive_stops.py after computing the nightly profile.

    `profile` is the full dict (stats + sl mults + target_rr + pause flag).
    """
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    sym_slug = _slug(symbol)
    filename = f"{stamp}_{sym_slug}.md"

    stats = (profile or {}).get("stats", {}) or {}
    tags = [
        "regime",
        f"symbol/{sym_slug}",
        f"bot/{bot}",
    ]
    if profile.get("pause"):
        tags.append("regime/paused")
    if profile.get("reason"):
        tags.append(f"regime/{_slug(profile.get('reason'))}")

    front = _front_matter(
        tags,
        symbol=symbol,
        bot=bot,
        date=stamp,
        long_sl_mult=profile.get("long_sl_mult"),
        short_sl_mult=profile.get("short_sl_mult"),
        target_rr=profile.get("target_rr"),
        trail_mult=profile.get("trail_mult"),
        pause=profile.get("pause"),
        reason=profile.get("reason"),
        skew=stats.get("skew"),
        kurtosis=stats.get("kurtosis"),
        vol_shift=stats.get("vol_shift"),
        tail_ratio=stats.get("tail_ratio"),
        mean_shift_std=stats.get("mean_shift_std"),
    )

    lines = [
        front,
        f"# Regime snapshot — {symbol} ({stamp})",
        "",
        f"bot `{bot}` · reason `{profile.get('reason', '?')}`"
        + (" · **PAUSED**" if profile.get("pause") else ""),
        "",
        "## Adaptive stop profile",
        "| Field | Value |",
        "|---|---|",
        f"| long_sl_mult | {profile.get('long_sl_mult')} |",
        f"| short_sl_mult | {profile.get('short_sl_mult')} |",
        f"| target_rr | {profile.get('target_rr')} |",
        f"| trail_mult | {profile.get('trail_mult')} |",
        f"| trail_arm_atr | {profile.get('trail_arm_atr')} |",
        f"| pause | {profile.get('pause')} |",
        "",
        "## Distribution stats (90-day rolling)",
        "| Stat | Value |",
        "|---|---|",
        f"| skew | {stats.get('skew')} |",
        f"| kurtosis | {stats.get('kurtosis')} |",
        f"| vol_shift | {stats.get('vol_shift')} |",
        f"| mean_shift_std | {stats.get('mean_shift_std')} |",
        f"| tail_ratio | {stats.get('tail_ratio')} |",
        f"| up_std | {stats.get('up_std')} |",
        f"| dn_std | {stats.get('dn_std')} |",
        f"| n_bars | {stats.get('n_bars')} |",
        "",
        "## Interpretation",
        f"- Skew `{stats.get('skew', '?')}` → ",
        f"- Vol shift `{stats.get('vol_shift', '?')}` → ",
        f"- Tail ratio `{stats.get('tail_ratio', '?')}` → ",
        "",
        "## Related",
        f"- [[symbols/{sym_slug}|{symbol}]]",
        "",
    ]
    return _write("regime", filename, "\n".join(lines))


def write_incident_note(kind: str, detail: str, extra: Optional[dict] = None) -> Optional[Path]:
    """Write a note for errors, kill-switch flips, DD halts, etc.
       `kind`: short slug ("last_error", "kill_switch", "account_dd_halt")
    """
    extra = extra or {}
    bot = extra.get("bot", "unknown")
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    kind_slug = _slug(kind)
    filename = f"{stamp}_{kind_slug}.md"

    tags = ["incident", f"kind/{kind_slug}", f"bot/{bot}"]

    front = _front_matter(
        tags,
        kind=kind,
        bot=bot,
        at=now.isoformat(timespec="seconds"),
    )

    lines = [
        front,
        f"# Incident — {kind}",
        "",
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} · bot `{bot}`",
        "",
        "## Detail",
        "```",
        str(detail)[:2000],
        "```",
        "",
    ]
    if extra:
        lines.append("## State")
        lines.append("| Key | Value |")
        lines.append("|---|---|")
        for k, v in extra.items():
            if k == "bot":
                continue
            lines.append(f"| {k} | {v} |")
        lines.append("")
    lines.append("## Root cause")
    lines.append("_(what happened, why, what to do next)_")
    lines.append("")
    return _write("incidents", filename, "\n".join(lines))


def list_recent(folder: str, limit: int = 20) -> list[dict]:
    """List recent notes in a vault folder — used by the unified dashboard panel."""
    root = _vault_root()
    if root is None:
        return []
    target = root / folder
    if not target.exists():
        return []
    out = []
    for p in sorted(target.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        out.append({
            "name": p.name,
            "folder": folder,
            "mtime": p.stat().st_mtime,
            "size": p.stat().st_size,
        })
    return out
