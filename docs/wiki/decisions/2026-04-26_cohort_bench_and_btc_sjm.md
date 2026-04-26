# 2026-04-26 — Cohort bench + BTC SJM filter

**Status:** final (executed)
**Last verified:** 2026-04-26
**Sources:** `config/deployed/_retired/`, `config/deployed/whale_BTC.json`, `/tmp/whale_oos.json`, Obsidian `research/2026-04-26_cohort_oos.md`

## What changed

Two deploy-time changes applied today, after a real OOS run + BTC head-to-head filter swap test:

### 1. Cohort bench

Three configs moved to `config/deployed/_retired/`:
- `whale_LINK.json`
- `whale_SOL.json`
- `whale_INJ.json`

xyz:CL was already in `_retired/` from prior cleanup.
**TIA kept** against the OOS verdict per Luca call — TIA was the closest to passing (OOS PF 1.43, OOS P&L +$68, only the random-benchmark gap failed). Worth observing live.

### 2. BTC SJM filter

`config/deployed/whale_BTC.json`:
- `use_1h_filter: false → true`
- `trend_filter_1h: "ema_cross" → "sjm"`
- Note field updated with swap context.

## Triggers

- **Cohort bench:** Losers report covering 2026-04-21 → 2026-04-26 showed 6 symbols at 0% WR (LINK, SOL, xyz:CL, OP, INJ, TIA — combined 13 trades, 0 wins, −$620). Seed #3 in `concepts/research-seeds.md` gated the action on a fresh OOS run.
- **OOS verdict:** All 4 testable cohort symbols (LINK, SOL, INJ, TIA) failed OOS via `research/whale_oos.py`. Verdict and per-symbol failure modes in `concepts/research-seeds.md` §#3 and Obsidian `research/2026-04-26_cohort_oos.md`. OP excluded from OOS due to HL 1h fetch returning empty for the 2000-bar request.
- **BTC swap:** 6 BTC trades since wiki bootstrap = 4W/2L but net −$253 because the 2 losers had FE < 1 ATR (entries wrong from the start). BTC's deployed had `use_1h_filter: false` — opted out of the 2026-04-21 per-symbol regime filter swap that lifted the rest of the portfolio +$1,545 over 41 days.

## BTC head-to-head OOS (41 days, params held fixed, only `trend_filter_1h` varies)

| Variant | Entries | WR | PF | Net $ | Δ vs current |
|---|---:|---:|---:|---:|---:|
| **CURRENT (filter OFF)** | 31 | 66.7% | 0.79 | −$231 | — |
| ema_cross | 24 | 55.9% | 0.57 | −$410 | −$179 |
| structure | 30 | 68.1% | 0.85 | −$162 | +$69 |
| both_agree | 23 | 57.6% | 0.62 | −$341 | −$110 |
| hma_slope | 31 | 66.7% | 0.79 | −$231 | $0 (HMA never vetoes BTC) |
| **sjm** | **13** | **79.2%** | **1.19** | **+$79** | **+$310** |

SJM cuts entries 60% but lifts WR to 79.2% and converts the strategy from net loser to net winner. Same methodology bar as the 2026-04-21 portfolio swap.

## Universe after these changes

13 active symbols: ARB, BTC, ENA, ETH, FARTCOIN, HYPE, LIT, OP, PENDLE, TIA, XRP, xyz:SILVER, ZEC.

Down from 16 (LINK, SOL, INJ retired). OP stays pending HL 1h fetch investigation.

## Caveats logged at decision time

- **BTC SJM**: 13 OOS trades is small N. Could be window-lucky. Reassess after 30+ more days of live paper data.
- **TIA kept** against OOS verdict — Luca's call. TIA's OOS was technically a fail (only +0.24 PF above random) but the live behavior is closer to break-even than the hard losers. Worth observing.
- **OP unresolved** — HL 1h fetch returns empty for the 2000-bar request (15m/4h fine). Could be intermittent API issue or cache problem. Re-OOS once fetch is fixed.
- **xyz:CL hygiene gap** — config existed in `_retired/` already, but its absence from `INSTRUMENTS` in `config/settings.py` means the deployer would have crashed if anyone re-elected CL. Worth a separate sanity-check pass on all `_retired/` symbols.

## Bot restart

The running bot loads configs once at `__init__`. These changes only take effect after restart (Luca handles).

## Re-election path

Any retired symbol can return via `research/whale_oos.py`:
- Pass: PF ≥ 1.2, WR ≥ 40%, n ≥ 15
- Beat random benchmark by ≥ 0.5 PF
- ±20% sensitivity holds
- Move config back to `config/deployed/` with updated `backtest_run_date`
