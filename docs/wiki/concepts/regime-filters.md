# Regime Filters (`trend_filter_1h`)

**Status:** active
**Last verified:** 2026-04-24 (filter values) / 2026-04-21 (OOS numbers)
**Sources:** `config/deployed/whale_*.json`, `core/features.py` (sjm_lookup_1h, hma_slope_lookup_1h), `research/filter_swap_test.py`, `research/whale_oos.py`

## Filters available

| Filter | Mechanism |
|---|---|
| `both_agree` | EMA cross AND ICT structure detector must agree |
| `structure` | ICT structure detector only |
| `hma_slope` | Hull Moving Average slope on 1h |
| `sjm` | Statistical Jump Model (Shu-Yu-Mulvey 2024, JoAM). λ jump penalty = 30 default |

## Per-symbol deployed assignment

Updated 2026-04-26 after BTC swap, LINK/SOL/INJ bench, and FARTCOIN/LIT/xyz:SILVER drift discovery.

**Active universe (10 symbols)** — assignments verified 2026-04-26 via per-symbol 41d head-to-head. Each row = `(use_1h_filter, trend_filter_1h)` actually deployed:

| Filter ON | Symbol(s) | Net $/41d (after deploy) |
|---|---|---:|
| `sjm` | **BTC** (deployed 2026-04-26), **ETH** (deployed 2026-04-26) | BTC +$79, ETH +$55 |
| `ema_cross` | **HYPE** (deployed 2026-04-26) | +$70 |
| `both_agree` | **ARB** (deployed 2026-04-26) | +$56 |
| `hma_slope` | XRP (unchanged from 2026-04-21) | — |

| Filter OFF (intentional) | Symbol(s) | Net $/41d at baseline | Why kept off |
|---|---|---:|---|
| (no 1h filter) | ZEC | +$630 | already optimal — every variant ties or hurts |
| (no 1h filter) | ENA | +$470 | best variant only +$19; not worth deploy risk |
| (no 1h filter) | PENDLE | +$67 | best variant only +$1 lift |
| (no 1h filter) | TIA | +$216 | no variant beats baseline |

| Status unknown | Symbol(s) | Reason |
|---|---|---|
| OP | HL 1h fetch returns empty for 2000-bar request — re-test once data is available |

**Inactive — config exists but missing from `INSTRUMENTS`** (silently skipped, drift discovered 2026-04-26):
- FARTCOIN (was filed under sjm)
- LIT (was filed under hma_slope)
- xyz:SILVER (was filed under both_agree)

To re-activate any of these: add an entry to `INSTRUMENTS` in `config/settings.py`. Files remain in `config/deployed/` so re-activation is trivial.

Symbols **fully retired** since 2026-04-21: kPEPE, SOL, LINK, INJ. Configs in `config/deployed/_retired/`. See [`decisions/2026-04-26_cohort_bench_and_btc_sjm.md`](../decisions/2026-04-26_cohort_bench_and_btc_sjm.md).

Verify current per-symbol filter against `config/deployed/whale_<SYMBOL>.json::trend_filter_1h` before acting — deployed configs are the source of truth.

## Head-to-head OOS (41 days, 10 symbols, params held fixed)

| Filter | Portfolio P&L |
|---|---|
| All-current (baseline) | −$1,103 |
| All-HMA | −$589 |
| All-SJM | −$666 |
| **Per-symbol best-of-three** | **+$442** |

Delta vs baseline: **+$1,545**. FARTCOIN alone went −$299 → +$98 under SJM.

## Known failure modes

- **HMA silences kPEPE** (n=0 trades in OOS). "Trading stops" is not the same as "edge discovered." If market regime shifts and kPEPE starts trading again, watch for whether the re-activated signal is profitable or reverts to pre-swap losses.
- **SJM silences SOL** (n=0 trades in OOS). Same caveat.
- 41 days is statistically underpowered. Reassess after another ~30 days of live paper data.

## When to revisit

- After 30+ days of new paper data accumulates
- If a symbol moves from winning → losing or vice versa
- Before any live flip (re-verify on the most recent OOS window)

Always run head-to-head through `research/filter_swap_test.py`. Don't default to "one filter fits all."

## Related

- Trigger event: [2026-04-21 regime filter swap decision](../decisions/2026-04-21_regime_filter_swap.md)
- Per-symbol pages (when added) should link back here for their `trend_filter_1h` rationale.
