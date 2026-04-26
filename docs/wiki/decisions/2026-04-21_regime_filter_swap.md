# 2026-04-21 — Per-Symbol Regime Filter Swap

**Status:** final (deployed)
**Last verified:** 2026-04-24
**Sources:** `research/filter_swap_test.py`, `research/whale_oos.py`, `core/features.py`, `config/deployed/whale_*.json`

## Decision

Swap `trend_filter_1h` per-symbol instead of using a single global filter. Deployed assignment:

- `hma_slope` → BTC, XRP, kPEPE, LIT
- `sjm` → HYPE, FARTCOIN, SOL
- Unchanged (`both_agree` / `structure`) → ZEC, ENA, xyz:CL (winning on current filter, don't overreact)

See [concepts/regime-filters.md](../concepts/regime-filters.md) for the live table and failure-mode notes.

## Trigger

2026-04-21: ZEC double-stopped (−$267) due to the `both_agree` 1h filter lagging 5h on pivot confirmation. Filter said UP while ZEC was in a clear downtrend.

## Research path

Survey of candidate filters:
- HMA slope (Hull Moving Average)
- Statistical Jump Model (Shu-Yu-Mulvey 2024, *Journal of Asset Management*)
- BOCPD (Bayesian online change-point detection)
- Time-series momentum

SJM was the most principled candidate but didn't universally beat HMA or the current filter — hence per-symbol best-of-three.

## Head-to-head OOS (41 days, 10 symbols, params held fixed, only `trend_filter_1h` varies)

| Filter | Portfolio P&L |
|---|---|
| All-current (baseline) | −$1,103 |
| All-HMA | −$589 |
| All-SJM | −$666 |
| **Per-symbol best-of-three** | **+$442** |

Delta vs baseline: **+$1,545**. FARTCOIN alone went −$299 → +$98 under SJM.

## Caveats logged at decision time

- 41 days is statistically underpowered. Reassess after another 30+ days of paper data.
- SJM's λ jump penalty is default 30. The paper recommends per-symbol CV tuning for additional lift — not yet done.
- HMA silences kPEPE (n=0 trades), SJM silences SOL (n=0 trades). "Stopped trading" ≠ "found edge." Watch if regime shifts and those symbols re-activate.

## Methodology note

Any future regime-filter work on this bot should start from this baseline and run head-to-head via `research/filter_swap_test.py`. Do not default to "one filter fits all."
