# 2026-04-12 — GARCH Sizing Rejected

**Status:** final (do not re-open without new evidence)
**Last verified:** 2026-04-24
**Sources:** `research/notebooks/garch_sizing_backtest.ipynb`, `research/notebooks/garch_sizing_backtest_stressed.ipynb`

## Decision

GARCH-based dynamic position sizing was evaluated for the (then-active) HyperLiquid Commodities Bot and rejected. The bot stays on fixed % sizing with the hard-rules risk gate as the circuit breaker. This decision carried over to the swing-trading-bot successor.

## Why

Walk-forward backtests on stressed historical windows showed:

- GARCH extrapolates **recent realized vol forward**. Fit on pre-stress data, it produces "size normal" forecasts that miss the first impact entirely.
- Earlier research that showed GARCH reducing size to 11% during stress was an artifact of **fitting on data that already included the spike** — look-ahead, not prediction.
- In calm windows GARCH correctly says "size up" (hits ceiling), but capped at 20% it adds zero value in calm regimes.
- In stressed windows it barely triggers until after the damage is done.

Net: GARCH failed to anticipate regime shifts, which is the one thing you'd hire it for.

## What to do instead

- Stay fixed % (currently 15% margin × 40 set leverage = 6× effective; see [sizing](../concepts/sizing.md)).
- Real risk control lives in the hard-rules gate ([risk-gate](../concepts/risk-gate.md)).
- If vol-scaled sizing ever comes up again, skip straight to realized-vol or ATR-ratio scaling — not GARCH.

## Do not re-pitch

Per durable user feedback: don't re-propose GARCH sizing unless specifically asked. The artifacts listed above are the work record — do not redo the analysis.
