"""
Standalone Discord command bot — listens for !commands and replies with bot state.

Reuses the DISCORD_BOT_TOKEN already configured in ~/.claude/channels/discord/.env
(same token as the Claude Code Discord plugin — fine, only one listener runs at a time).

Commands:
  !status   — overall bot health + balance
  !pos      — open positions
  !signals  — which symbols are close to firing
  !pnl      — today's P&L
  !help     — this list
"""
import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timezone

import discord
import requests

DASHBOARD = "http://localhost:5070"
ENV_PATH = Path.home() / ".claude/channels/discord/.env"


def _load_token() -> str:
    tok = os.getenv("DISCORD_BOT_TOKEN", "")
    if tok:
        return tok
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"DISCORD_BOT_TOKEN not set and {ENV_PATH} missing")


def _fetch_state() -> dict | None:
    try:
        r = requests.get(f"{DASHBOARD}/api/state", timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _cmd_status(s: dict) -> str:
    if not s:
        return "⚠️ dashboard not responding (is it running on :5070?)"
    bal = s.get("balance", 0)
    start = s.get("starting_balance", 10000)
    pct = (bal - start) / start * 100 if start else 0
    lines = [
        f"**Balance:** ${bal:,.2f} ({pct:+.2f}% from start)",
        f"**Open positions:** {s.get('open_count', 0)} / {s.get('max_concurrent', 4)}",
        f"**Daily P&L:** ${s.get('daily_pnl', 0):+,.2f}",
        f"**Mode:** {s.get('mode', '?')} · {s.get('network', '?')}",
        f"**Kill switch:** {'🔴 ON' if s.get('kill_switch') else '🟢 off'}",
        f"**Last refresh:** {s.get('last_refresh', '?')}",
    ]
    return "\n".join(lines)


def _cmd_pos(s: dict) -> str:
    if not s:
        return "⚠️ dashboard not responding"
    pos = s.get("positions", [])
    if not pos:
        return "No open positions."
    lines = ["**Open positions:**"]
    for p in pos:
        sym = p.get("symbol", "?")
        side = p.get("side", "?")
        entry = p.get("entry_price", 0)
        cur = p.get("current_price", 0)
        pnl = p.get("unrealized_pnl", 0)
        bars = p.get("bars_held", "?")
        mx = p.get("max_hold_bars", "?")
        lines.append(f"`{sym}` {side} @ {entry:.4f} → {cur:.4f}  P&L **${pnl:+,.2f}**  ({bars}/{mx} bars)")
    return "\n".join(lines)


def _cmd_signals() -> str:
    """Run the proximity diagnostic and return the top results."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["/Users/lucaneto/swing-trading-bot/.venv/bin/python",
             "/tmp/signal_proximity.py"],
            cwd="/Users/lucaneto/swing-trading-bot",
            stderr=subprocess.STDOUT,
            timeout=45,
        ).decode()
    except Exception as e:
        return f"⚠️ signal check failed: {e}"
    # compress the output: keep only 🔓 lines + next line
    blocks = []
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("🔓"):
            blocks.append(ln)
            if i + 1 < len(lines):
                blocks.append(lines[i + 1].strip())
            if i + 2 < len(lines):
                blocks.append(lines[i + 2].strip())
    if not blocks:
        return "⛔ No symbols have an open side right now — all gated by 1h/4h filters."
    header = "**Symbols with open gate** (still need trigger):\n"
    return header + "```" + "\n".join(blocks)[:1800] + "```"


def _cmd_pnl(s: dict) -> str:
    if not s:
        return "⚠️ dashboard not responding"
    lines = [f"**Today's P&L:** ${s.get('daily_pnl', 0):+,.2f}"]
    hist = s.get("trade_history", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todays = [t for t in hist if str(t.get("exit_time", "")).startswith(today)]
    if todays:
        lines.append(f"**Closed today:** {len(todays)}")
        for t in todays[-5:]:
            sym = t.get("symbol", "?")
            pnl = t.get("pnl", 0)
            rsn = t.get("exit_reason", "?")
            lines.append(f"  `{sym}` **${pnl:+,.2f}** ({rsn})")
    return "\n".join(lines)


HELP = (
    "**Swing bot commands:**\n"
    "`!status`  — balance, open count, daily P&L\n"
    "`!pos`     — open positions\n"
    "`!signals` — symbols with open entry gate\n"
    "`!pnl`     — today's P&L + recent closes\n"
    "`!help`    — this message"
)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    c = msg.content.strip().lower()
    if not c.startswith("!"):
        return

    if c == "!status":
        await msg.channel.send(_cmd_status(_fetch_state()))
    elif c == "!pos":
        await msg.channel.send(_cmd_pos(_fetch_state()))
    elif c == "!signals":
        await msg.channel.send("⏳ checking…")
        await msg.channel.send(_cmd_signals())
    elif c == "!pnl":
        await msg.channel.send(_cmd_pnl(_fetch_state()))
    elif c in ("!help", "!commands"):
        await msg.channel.send(HELP)


if __name__ == "__main__":
    client.run(_load_token())
