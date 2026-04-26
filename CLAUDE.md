# Swing Trading Bot — Whale Strategy

Replaces the commodities-bot + crypto-bot momentum strategies.
Based on 58bro.eth + nervousdegen.eth wallet pattern research (2026-04-16).

## Wiki — read this first

`docs/wiki/` is the canonical, maintained knowledge base (Karpathy-style LLM wiki).

- Start at [`docs/wiki/index.md`](docs/wiki/index.md) to find the right page.
- Conventions + ingest/query/lint workflows are in [`docs/wiki/WIKI.md`](docs/wiki/WIKI.md).
- When this file and the wiki disagree, **the wiki wins** — its pages carry `Last verified` dates; the sections below this one may have drifted.

## What it does

- 5m entry precision, multi-day holds (1–5 days max)
- Four entry types per symbol: `rsi_bounce`, `bb_touch`, `ema_bounce`, `swing_pivot`
- Partial TP1 (close 50% at TP1 price), SL moves to breakeven after TP1 hit
- Optional trailing stop for the remainder (only activates after 1× offset move in favor)
- Max hold cap (1/3/5 days) forces close if the setup stalls
- Per-symbol config from `config/deployed/whale_<SYMBOL>.json` (elected from grid backtest)

## 58bro position sizing model

| Parameter | Value | Effect |
|--|--|--|
| `margin_pct` | 0.15 | 15% of account margin per trade |
| `set_leverage` | 40 | High set lev → large notional per dollar of margin |
| Effective leverage | 6.0× | Real risk on equity |
| Free capital | 85% | Remainder of equity stays idle as cushion |
| Liq distance | ~17% per position | Wider as more positions open |

The 40× is capital efficiency, not risk. The real risk level is `margin_pct × set_leverage = 6×`.

## Symbol universe

Whatever has an active config in `config/deployed/whale_*.json` is in the universe. The risk gate's `max_concurrent_positions=4` enforces 58bro-style concurrency discipline — many candidates, tight concurrency.

Currently deployed (as of 2026-04-24): `xyz:SILVER`, `BTC`, `ETH`, `HYPE`, `XRP`, `ZEC`, `ENA`, `SOL`, `LIT`, `FARTCOIN`, `ARB`, `INJ`, `LINK`, `OP`, `PENDLE`, `TIA`. Retired configs live in `config/deployed/_retired/`.

## Risk gate

Deterministic rules, never AI. The values below are the live-ready target. **Paper mode currently runs with global halts disabled** to collect data through losing stretches — see [`docs/wiki/concepts/risk-gate.md`](docs/wiki/concepts/risk-gate.md) for the live table and the pre-live restore checklist.

- Max concurrent positions: **4**
- Max daily loss: 5% → kill switch *(paper: disabled)*
- Max account drawdown from peak: 15% → halt *(paper: disabled)*
- Max consecutive losses: 5 → halt *(paper: disabled)*
- Per-symbol 24h loss cap: 2% → symbol paused 24h *(enabled in both paper and live)*
- HyperLiquid commission: 0.030% per side (HL tier 0 crypto perp, 50/50 maker/taker realistic estimate — was 0.006% until 2026-04-20, that was an institutional blend + HIP-3 discount assumption that no longer applies since SILVER/ETH were demoted and the bot is now 100% native crypto perps)

## Architecture

```
main.py — cycle every 5m candle close
  ├─ core/data.py — HL candle fetch + 1h trend precompute
  ├─ core/features.py — EMA / RSI / ATR / Bollinger
  ├─ strategies/whale_swing.py — evaluates latest bar, returns EntrySignal or None
  ├─ core/risk.py — deterministic gate
  ├─ core/execution.py — PaperTrader: partial TP1, SL-to-BE, trail, max_hold
  └─ core/alerts.py — Discord webhooks (entry / exit / status)
```

Shared modules (reused from `../shared/`):
- `hl_client.py`, `vault_writer.py`, `reporting.py`

## Commands

```bash
source .venv/bin/activate
python main.py                             # run paper mode
nohup python main.py >> logs/bot_live.log 2>&1 & disown
tail -f logs/bot_live.log
```

## Hard rules

- `paper_trading: true` by default — live mode requires explicit `paper_trading: false`
- Risk gate is always deterministic, never AI
- Per-symbol strategy params come from `config/deployed/*.json` — never hardcoded
- All entries size from `sizing.margin_pct × set_leverage` — never bypass sizing
- Obsidian trade notes auto-write via `shared/vault_writer.py`

## Commodities-bot + crypto-bot status

Both legacy bots are retained in-repo as reference but will not run going forward. Pine scripts (`*.pine`) are archived — momentum is retired.
