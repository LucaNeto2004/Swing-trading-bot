# Wiki Index

Catalog of every page. Read this first when answering questions about the bot.

## Concepts

- [risk-gate](concepts/risk-gate.md) — live concurrency + halt posture, pre-live restore checklist
- [sizing](concepts/sizing.md) — 58bro model (margin_pct × set_leverage = 6× eff), why fixed, why not GARCH
- [regime-filters](concepts/regime-filters.md) — per-symbol `trend_filter_1h` assignment + head-to-head OOS numbers
- [research-seeds](concepts/research-seeds.md) — open research questions worth testing next cycle (compression filter, weekend regime)

## Decisions

- [2026-04-12 — GARCH sizing rejected](decisions/2026-04-12_garch_rejected.md) — walk-forward showed GARCH can't anticipate regime shifts
- [2026-04-16 — Whale-strategy pivot](decisions/2026-04-16_whale_pivot.md) — retired momentum bots, built new whale swing bot
- [2026-04-21 — Per-symbol regime filter swap](decisions/2026-04-21_regime_filter_swap.md) — HMA/SJM/current mixed deploy after 41-day OOS
- [2026-04-26 — Cohort bench + BTC SJM](decisions/2026-04-26_cohort_bench_and_btc_sjm.md) — LINK/SOL/INJ retired; BTC swapped to SJM (filter-OFF → SJM, +$310/41d)
- [2026-04-26 — Filter swap baseline snapshot](decisions/2026-04-26_filter_swap_baseline_snapshot.md) — pre-change BTC/HYPE/ETH/ARB configs + predicted uplifts; **compare on 2026-04-30**

## Symbols

- [xyz_SILVER](symbols/xyz_SILVER.md) — London+NY weekdays only; xyz-deployer HIP-3 liquidity caveat

## Meta

- [WIKI.md](WIKI.md) — conventions and workflows
- [log.md](log.md) — chronological record
