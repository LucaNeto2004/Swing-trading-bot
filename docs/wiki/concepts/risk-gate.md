# Risk Gate

**Status:** active (paper posture)
**Last verified:** 2026-04-24 (against `config/settings.py::RiskConfig`)
**Sources:** `config/settings.py`, `core/risk.py`

## Current posture — paper, always-on

| Field | Value | Status | Notes |
|---|---|---|---|
| `max_concurrent_positions` | **4** | enabled | total cap across any mix (raised 2→4 on 2026-04-22) |
| `max_crypto_concurrent` | 4 | sub-cap disabled | set equal to total → only total binds |
| `max_commodity_concurrent` | 4 | sub-cap disabled | set equal to total → only total binds |
| `max_daily_loss_pct` | 1.0 | **disabled** | accepts up to −100% daily |
| `max_account_drawdown_pct` | 1.0 | **disabled** | no peak-to-trough halt |
| `max_consecutive_losses` | 9999 | **disabled** | no consec-loss halt |
| `per_symbol_daily_loss_pct` | **0.02** | **enabled** | pauses one symbol 24h after losing 2% of equity |
| `per_symbol_cap_enabled` | `True` | enabled | toggle for the above |
| `commission_pct` | **0.00030** | enforced | HL tier-0 crypto perp, 50/50 maker/taker |

Global halts are off by design — the paper bot must run through losing stretches to collect data. The per-symbol 24h cap isolates one bleeding symbol without stopping the rest.

## Pyramiding constraints

From `config/settings.py::PyramidConfig`:

- `drawdown_lock_pct` = 5.0 — no adds if account DD from peak exceeds this
- `banned_symbols` = `("kPEPE", "FARTCOIN", "ENA", "ZEC")` — pyramid disabled (ZEC added 2026-04-19 after backtest showed PF 2.24→1.65)

## Pre-live checklist

Before any `paper_trading: false` flip, restore:

- `max_daily_loss_pct` → 0.05
- `max_account_drawdown_pct` → 0.15
- `max_consecutive_losses` → 5
- `per_symbol_daily_loss_pct` → keep at 0.02 (useful in live too)
- Re-verify commission matches current HL tier (may differ from 0.00030)

## History

- **2026-04-22** — `max_concurrent_positions` 2→4. Group sub-caps equalised to total.
- **2026-04-21** — `per_symbol_daily_loss_pct` 0.02 added after ZEC bleed event.
- **2026-04-20** — `commission_pct` bumped 0.00006 → 0.00030 (SILVER/ETH demoted from HIP-3 institutional blend → realistic retail tier).
- **2026-04-16** — Global halts disabled for paper-data collection posture.

## Hard rule

Risk gate is **always deterministic, never AI**. See `/CLAUDE.md`.
