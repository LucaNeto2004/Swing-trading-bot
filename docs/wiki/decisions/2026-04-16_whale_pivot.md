# 2026-04-16 — Whale-Strategy Pivot

**Status:** final (executed)
**Last verified:** 2026-04-24
**Sources:** `/CLAUDE.md`, `strategies/whale_swing.py`, `config/deployed/whale_*.json`

## Decision

Retire the two momentum bots (`trading/commodities-bot/` momentum, `trading/crypto-bot/` momentum_v15). Build and run a single new **swing-trading-bot** implementing a whale-mirror swing strategy.

## Trigger

Research on 58bro.eth + nervousdegen.eth HyperLiquid wallet patterns:
- Both trade 5m entries, multi-day holds (1–5 days).
- Partial TP1, SL-to-BE after TP1, optional trailing for remainder.
- Wide symbol universe, **tight concurrency**. Many candidates, few simultaneous positions.
- Fixed % margin × high set leverage (40×) → 6× effective leverage on equity.

Momentum bots had diverged from Pine-script parity (heavy pyramiding, gates relaxed to mirror TV behavior). The whale pattern was both more disciplined and backtested with better OOS metrics on the same symbols.

## What changed

- **Repo**: new `/Users/lucaneto/swing-trading-bot/` — fresh tree, shared modules reused from `../shared/` (`hl_client.py`, `vault_writer.py`, `reporting.py`).
- **Strategy**: `strategies/whale_swing.py` — four entry types (`rsi_bounce`, `bb_touch`, `ema_bounce`, `swing_pivot`). Partial TP ladder (TP1/TP2/TP3 scale-out), SL-to-BE after TP1, max_hold cap (1/3/5 days).
- **Universe**: 11 symbols seed (SILVER, BTC, ETH, HYPE, ZEC, XRP, kPEPE, FARTCOIN, BIO, ORDI, LIT). Grown since (ARB, ENA, INJ, LINK, OP, PENDLE, SOL, TIA deployed). Verify against `config/deployed/`.
- **Sizing**: `margin_pct=0.15 × set_leverage=40 = 6× effective`. See [sizing](../concepts/sizing.md).
- **Risk gate**: global halts disabled for paper-data collection; per-symbol 24h cap as the single live guard. See [risk-gate](../concepts/risk-gate.md).

## What was retired

- Commodities-bot momentum — code retained in-repo as reference, not running.
- Crypto-bot momentum_v15 — same.
- Pine scripts (`*.pine`) archived. Momentum is not coming back.

## Hard constraints carried over

- Never modify `*.pine` without asking.
- Risk gate always deterministic, never AI.
- Paper trading default; live flip requires explicit `paper_trading: false` + pre-live checklist.

## What it did NOT change

- GARCH rejection stays in force (fixed sizing). See [2026-04-12 garch-rejected](2026-04-12_garch_rejected.md).
- OOS validation pipeline (IS/OOS split, quartile check, random benchmark, ±20% sensitivity) — any new symbol must still pass `research/commod_oos.py` or equivalent.
