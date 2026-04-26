# Position Sizing

**Status:** active
**Last verified:** 2026-04-24
**Sources:** `config/settings.py` (SizingConfig), `strategies/whale_swing.py`, `/CLAUDE.md`

## 58bro model

Based on 58bro.eth + nervousdegen.eth wallet pattern research (2026-04-16).

| Parameter | Value | Role |
|---|---|---|
| `margin_pct` | 0.15 | 15% of account margin per trade |
| `set_leverage` | 40 | Capital efficiency, not risk |
| **Effective leverage** | **6.0×** | `margin_pct × set_leverage` — the real risk |
| Free capital | 85% | Cushion against drawdown + liquidation |
| Liq distance | ~17% per position | Widens as more positions open |

The 40× is notional per dollar of margin, not risk. The real risk number is 6× effective.

## Rules

- Every entry sizes from `margin_pct × set_leverage`. No ad-hoc overrides.
- Position size is fixed % of equity, not vol-adjusted. See [garch-rejected decision](../decisions/2026-04-12_garch_rejected.md) for why.
- Pyramiding is a separate concern — see `config/settings.py::PyramidConfig` and [risk-gate](risk-gate.md#pyramiding-constraints).

## Why fixed (not vol-scaled)

GARCH-based dynamic sizing was evaluated Apr 2026 and rejected. Walk-forward showed GARCH extrapolates recent realized vol forward — fit-on-pre-stress data produces "size normal" forecasts that miss the first impact entirely. In calm regimes GARCH hits the 25% ceiling; capped at 20% it adds zero value in calm windows and barely triggers in stressed ones.

Don't re-pitch GARCH. If vol sizing comes up again, skip to a realized-vol or ATR-ratio scaler — not GARCH.

## Relation to risk gate

Sizing determines per-trade notional; the risk gate caps aggregate (concurrency, daily loss, account DD, per-symbol 24h loss). Both must be respected — sizing does not bypass the gate, and the gate does not override sizing.
