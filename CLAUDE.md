# Swing Trading Bot — Whale Strategy

Replaces the commodities-bot + crypto-bot momentum strategies.
Based on 58bro.eth + nervousdegen.eth wallet pattern research (2026-04-16).

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

All 11 symbols the backtest covered. The risk gate's `max_concurrent_positions=2` enforces 58bro-style concurrency discipline — many candidates, tight concurrency.

- `xyz:SILVER`, `BTC`, `ETH`, `HYPE`, `ZEC`, `XRP`, `kPEPE`, `FARTCOIN`, `BIO`, `ORDI`, `LIT`

## Risk gate

Deterministic rules, never AI:

- Max concurrent positions: **2**
- Max daily loss: 5% → kill switch
- Max account drawdown from peak: 15% → halt
- Max consecutive losses: 5 → halt
- HyperLiquid blended commission: 0.006% per side

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
