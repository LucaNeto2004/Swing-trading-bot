# 2026-04-26 — Filter swap baseline snapshot (compare on Thursday 2026-04-30)

**Status:** open — comparison pending
**Last verified:** 2026-04-26
**Compare on:** 2026-04-30 (Thursday) — gives 4 days of forward paper data
**Sources:** `config/deployed/whale_<SYM>.json` (current), this doc (pre-change)

## What this doc is for

Today (2026-04-26) we changed the 1h filter on 4 symbols. This file captures the **pre-change state** so we can A/B compare **predicted** (backtest) vs **realized** (live paper) over the next 4 trading days.

## Pre-change configs (revert path if needed)

| Symbol | `use_1h_filter` | `trend_filter_1h` | Notes |
|---|---|---|---|
| BTC | **false** | "ema_cross" | filter was off entirely; ema_cross was vestigial |
| HYPE | **false** | "ema_cross" | same — set but ignored |
| ETH | **false** | "ema_cross" | same |
| ARB | **false** | "ema_cross" | same |

XRP was already on `hma_slope` with `use_1h_filter: true` since the 2026-04-21 swap — left untouched.

## Post-change configs (active as of 2026-04-26 ~20:30 UTC)

| Symbol | `use_1h_filter` | `trend_filter_1h` | Predicted Δ /41d |
|---|---|---|---:|
| BTC | true | **sjm** | **+$310** |
| HYPE | true | **ema_cross** (activated) | **+$99** |
| ETH | true | **sjm** | **+$133** |
| ARB | true | **both_agree** | **+$156** |
| **Total** | | | **+$698 / 41d** |

(Note: per-day expected uplift ≈ $698/41 = ~$17/day across these 4 symbols. Over 4 days expect ~$68 uplift from the swaps alone, dwarfed by normal trade-level noise. Forward-test signal will be noisy; treat as a directional check, not a clean validation.)

## Per-symbol predicted vs current-state baseline

For each, the `backtest filter-OFF $/41d` is what the current 6-day live experience was hinting at; `backtest best-variant $/41d` is what we expect with the swap active.

### BTC
- Backtest baseline (filter OFF): **−$231 / 41d**
- Backtest with SJM: **+$79 / 41d**
- Live last 6 days (under filter OFF): **−$253** in 6 trades
- Most recent SL hits had FE < 1 ATR — entries were wrong, exactly the case SJM would veto

### HYPE
- Backtest baseline (filter OFF): **−$29 / 41d**
- Backtest with ema_cross: **+$70 / 41d**
- Live last 6 days (under filter OFF): **+$141** — outperforming backtest baseline (sample noise)
- Surprising: SJM was the WORST variant for HYPE despite wiki memory claiming HYPE → SJM

### ETH
- Backtest baseline (filter OFF): **−$79 / 41d**
- Backtest with SJM: **+$55 / 41d**
- Live last 6 days (under filter OFF): **−$152** in 3 trades — worse than backtest baseline pace
- Sample is tiny (n=3 live), 1 SL hit at −$160 dragging it

### ARB
- Backtest baseline (filter OFF): **−$100 / 41d**
- Backtest with both_agree: **+$56 / 41d**
- Live last 6 days (under filter OFF): **−$47** in 5 trades
- Best variant ema_cross was nearly tied (+$10), so both_agree is the clear pick

## Comparison protocol (run on Thursday 2026-04-30)

1. **Pull trade history** for 2026-04-26 ~20:30 UTC → 2026-04-30 EOD per symbol.
2. **Compare to expected pace.** Each symbol's daily P&L should be in the direction of the predicted Δ. Don't expect tight match — sample is small.
3. **Flag regressions.** If any swapped symbol is **net more negative** than its pre-swap 6-day pace, investigate before extending the experiment further.
4. **Decision criteria for keeping vs reverting:**
   - **KEEP** if symbol is net positive OR is materially less negative than its baseline 6-day pace.
   - **HOLD** (don't decide yet) if N < 8 trades — more data needed.
   - **REVERT** only if symbol is net more negative than baseline AND N ≥ 8 trades. Use the pre-change values from the table above.
5. **Update this doc** with the realized numbers and the per-symbol verdict.
6. **Append to `docs/wiki/log.md`** with the comparison summary.

## Realized results (fill on 2026-04-30)

| Symbol | Trades | Net P&L | Daily pace | vs predicted | Verdict |
|---|---:|---:|---:|---|---|
| BTC | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| HYPE | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| ETH | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| ARB | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

## Revert commands (if any symbol fails the comparison)

For symbol `X`, edit `config/deployed/whale_X.json`:
- `use_1h_filter: true → false`
- `trend_filter_1h`: revert to `"ema_cross"` (the vestigial value, harmless when use_1h_filter is false)
- Append revert reason to the `note` field
- Restart the bot

## Cross-references

- [2026-04-26 cohort bench + BTC SJM](2026-04-26_cohort_bench_and_btc_sjm.md) — the BTC half of today's changes
- [`concepts/research-seeds.md` §#5](../concepts/research-seeds.md) — the use_1h_filter audit that produced these swaps
- [`concepts/regime-filters.md`](../concepts/regime-filters.md) — current per-symbol filter assignment
