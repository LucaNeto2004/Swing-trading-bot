# Research Seeds

**Status:** active (rolling doc)
**Last verified:** 2026-04-26
**Sources:** day-of observation + `logs/daily/`

Open research questions worth investigating in the next research cycle. Promote a closed seed into a `decisions/` page when it's tested and resolved.

---

## #1 — Compression / chop filter for whale_swing

**Status:** open
**Opened:** 2026-04-26
**Origin:** −$276 Sunday after a +$646 weekday run.

**Observation.** Sunday 2026-04-26: 8/10 trades lost. 8 of 9 un-gated LONG entries fired when BTC's last 1h close was UP, so the existing BTC-1h-confirm gate cannot explain the bleed (it would have only saved −$17.88). The actual condition: BTC sat in a **0.4% range over 6 hours** (77,800–78,100). Mean-reverting entries triggered inside the range and got stopped on the other side; trend-following entries fired on local breakouts that mean-reverted before extending.

**Hypothesis.** A compression / range-bound filter would skip entries when realized vol or recent range is below a threshold. Single-bar BTC direction is too noisy to catch this — chop oscillates across the gate.

**Candidate filters to test.**
- ATR ratio: `current_ATR / ATR_MA(N)` < threshold → skip
- Range filter: skip if last N bars sit inside an X% range
- The old momentum bots used `choppy_mult = ATR < 0.8 × ATR_MA` — same family, known to work in trend regimes
- BBW (Bollinger Band Width) compression — skip on width below percentile

**Next test.** Backtest each variant against the current per-symbol deployed configs, held-params head-to-head over the last ~41 days (same window used for the regime filter swap). Use `research/filter_swap_test.py` as the harness. Compare to baseline P&L.

**Open questions.**
- Per-symbol or global threshold?
- Apply to all `entry_type`s or only `bb_touch` / `rsi_bounce` (mean-reverting)?
- Does it interact with the per-symbol `trend_filter_1h` choice?

---

## #2 — Weekend regime treatment

**Status:** open
**Opened:** 2026-04-26
**Origin:** Sat 2026-04-25 (−$108) + Sun 2026-04-26 (−$276) vs strong weekday performance.

**Observation.** Two consecutive weekend losing days. Crypto weekends are typically lower-volume and more chop-prone (no equity flow, no oncall traders, no macro events). The bot does not currently distinguish weekend vs weekday for crypto symbols (xyz:SILVER has its session filter; nothing else does).

**Hypothesis.** Weekend regimes may need either (a) tighter filters (compression gate, see #1), (b) a hard skip rule like xyz:SILVER, or (c) reduced position sizing.

**Next test.**
- Pull last ~90 days of bot data and split P&L by day-of-week. Is weekend systematically worse?
- If yes, A/B: skip-weekends vs current.
- Caveat: small sample (the bot is < 2 weeks old in this config).

---

## #3 — 0% WR cohort: bench candidates pending OOS

**Status:** **CLOSED 2026-04-26.** OOS confirmed bench. LINK/SOL/INJ retired; TIA kept per Luca; xyz:CL was already retired; OP deferred. See [`decisions/2026-04-26_cohort_bench_and_btc_sjm.md`](../decisions/2026-04-26_cohort_bench_and_btc_sjm.md).
**Opened:** 2026-04-26
**Origin:** Losers report covering 2026-04-21 → 2026-04-26.

**Observation.** Six deployed symbols have produced **zero wins** over the last 6 trading days:

| Symbol | Trades | Wins | Losses | Net |
|---|---:|---:|---:|---:|
| LINK | 4 | 0 | 4 | −$180 |
| SOL | 2 | 0 | 2 | −$128 |
| xyz:CL | 2 | 0 | 2 | −$113 |
| OP | 2 | 0 | 2 | −$89 |
| INJ | 2 | 0 | 2 | −$73 |
| TIA | 1 | 0 | 1 | −$37 |
| **Total** | **13** | **0** | **13** | **−$620** |

By contrast, the bot's edge is concentrated in ZEC (+$711), ENA (+$398), HYPE (+$141), XRP (+$61), PENDLE (+$23). Net portfolio +$263 means the 0% WR cohort is the difference between a strong week and break-even.

**Caveat — sample size.** 13 trades across 6 symbols (1–4 trades each) is too small to conclude these configs are broken. Could be:
- Genuine regime mismatch (the configs were OOS'd in a different vol regime)
- Bad luck / unlucky window
- Collateral damage from this weekend's compression event (compounds with seeds #1 and #2)

**Proposed action — gated.**
1. Run fresh OOS via `research/whale_oos.py` for each of the 6 symbols using current deployed params, on the last 21–30 days of data.
2. Pass criteria: PF ≥ 1.2, WR ≥ 40%, n ≥ 15 trades. Same standards used in the original validation.
3. If a symbol **fails** OOS → move its config to `config/deployed/_retired/`. File a per-symbol decision doc.
4. If a symbol **passes** OOS → keep deployed; the recent losses were unlucky window.
5. Do **not** bench any symbol on observed paper performance alone. The OOS gate exists for this exact reason.

**Why deferred.** Until the OOS run is done, we don't know which of the 6 are genuinely broken vs unlucky. Acting now would risk benching a symbol whose edge is intact but whose recent window was rough — exactly the over-fitting failure mode the wiki is supposed to prevent.

### OOS results (2026-04-26)

Ran via `research/whale_oos.py` with `LIVE_SYMBOLS` overridden to the cohort. Pipeline searches a 48-config grid for the IS-best, then evaluates that elected config on OOS.

| Sym | IS PF | OOS PF | OOS $ | Verdict |
|---|---:|---:|---:|---|
| LINK | 7.05 | 0.62 | −$19 | FAIL — overfitting (IS 7→0.6) + n=8 underpowered |
| SOL | 1.74 | 0.51 | −$114 | FAIL — distribution shift, sens broke at 0.8× |
| INJ | 3.97 | — | $0 | FAIL — n=0 OOS (config never triggers on recent data) |
| TIA | 1.05 | 1.43 | +$68 | FAIL — only +0.24 PF above random benchmark |

**OP excluded** — HL 1h fetch returns empty for the 2000-bar request (15m and 4h work). Cannot OOS until fixed.
**xyz:CL excluded** — missing from `INSTRUMENTS` in `config/settings.py`. Config exists but no instrument metadata = the bot is trading something it doesn't have specs for. Bench on hygiene grounds.

**Recommended action (pending Luca):**
- Bench LINK, SOL, INJ, TIA → move to `config/deployed/_retired/`
- Bench xyz:CL on metadata gap
- Investigate OP HL fetch issue, then re-OOS

Universe would shrink 16 → 11 active. Closer to 58bro's "many candidates, tight concurrency" intent.

Re-election: any benched symbol can return via fresh OOS pass meeting PF ≥ 1.2, WR ≥ 40%, n ≥ 15, beats random by ≥ 0.5 PF.

Full breakdown: `/tmp/whale_oos.json` and `obsidian://research/2026-04-26_cohort_oos.md`.

---

## #4 — LONG/SHORT performance asymmetry

**Status:** open
**Opened:** 2026-04-26
**Origin:** Losers report covering 2026-04-21 → 2026-04-26.

**Observation.**

| Side | N | WR | Net |
|---|---:|---:|---:|
| SHORT | 20 | 60.0% | **+$273** |
| LONG | 28 | 39.3% | −$10 |

The bot is **net flat on longs and net +$273 on shorts** despite taking 40% more long trades. Shorts carry essentially the entire portfolio edge over this window.

**Hypotheses (not tested).**
- **Sample noise.** 28 long trades is small. Reverses next week if the underlying regime flips.
- **Regime-specific.** Recent BTC compression has favored short-fade entries over long-bounce entries. Would not generalize to a trending-up regime.
- **Entry-type imbalance.** Long signals may be coming disproportionately from `bb_touch` / `rsi_bounce` (mean-reverting), which are exactly the entries that get killed in chop. Worth checking whether the entry-type mix differs between sides.
- **Per-symbol asymmetry.** Some symbols may have config that only fires longs (e.g. `direction: long`), so the long pool is dominated by a subset of underperformers (LINK / SOL / OP / etc. from #3).

**Next test.**
- Wait 30+ more trading days for sample to grow. Re-pull the same breakdown.
- Cross-tab side × entry_type × symbol. If the long bias correlates with the 0% WR cohort, this seed collapses into #3.
- If asymmetry persists across the whole universe and the trending regime returns, investigate long-side entry conditions.

**Don't act on this until N ≥ 60 per side.** Right now this is a flag, not a finding.

---

## #5 — `use_1h_filter` audit across the full universe

**Status:** **CLOSED 2026-04-26** — full universe tested. HYPE/ETH/ARB deployed. ZEC/ENA/PENDLE/TIA hold (current already optimal). OP deferred (HL fetch error).
**Opened:** 2026-04-26

**Origin.** Dashboard symbol-card upgrade revealed that **8 of 10 active symbols have `use_1h_filter: false`** in their deployed configs. The `trend_filter_1h` field is set per symbol (e.g. HYPE: `"ema_cross"`, ZEC: `"ema_cross"`, etc.), but the filter is silently ignored when `use_1h_filter` is false. Only BTC (after today's swap) and XRP have a 1h filter actually engaged.

**Why this matters.** The 2026-04-21 regime filter swap decision is documented as the source of +$1,545 portfolio uplift. But for any symbol that was supposed to get that uplift via a filter swap, the change cannot have applied if `use_1h_filter` was false. Either the swap test was run on filter-OFF baselines (so the "winning" filter was never measured against itself-on), or the deploy of the swap missed setting `use_1h_filter: true`.

**Which symbols are affected (use_1h_filter=False as of 2026-04-26):**
HYPE, ZEC, ENA, ETH, ARB, PENDLE, TIA, OP.

**Tested:**

### HYPE (2026-04-26)

41-day head-to-head, params held fixed, only `trend_filter_1h` varies (with `use_1h_filter` forced ON for variants):

| Variant | Entries | WR | PF | Net $ | Δ |
|---|---:|---:|---:|---:|---:|
| CURRENT (filter OFF) | 33 | 77.6% | 0.93 | −$29 | — |
| **ema_cross** | 28 | 78.6% | 1.26 | **+$70** | **+$99** |
| structure | 28 | 78.6% | 1.01 | +$2 | +$31 |
| both_agree | 27 | 80.5% | 1.26 | +$71 | +$99 |
| hma_slope | 14 | 73.7% | 0.52 | −$124 | −$96 |
| **sjm** | 23 | 69.7% | 0.59 | **−$166** | **−$137** |

**Verdict:** `ema_cross` (or `both_agree`, identical uplift). **Do NOT use SJM on HYPE** — worst of all variants despite what `concepts/regime-filters.md` currently claims. Wiki memory was fiction; the 2026-04-21 swap result for HYPE never landed in production.

**Recommended HYPE config edit:**
- `use_1h_filter: false → true`
- `trend_filter_1h: "ema_cross"` (already the default value, just needs to be activated)

### Full universe results (2026-04-26 batch)

41-day head-to-head per symbol, params held fixed, only `trend_filter_1h` varies (with `use_1h_filter` forced ON for variants). Deploy threshold: Δ ≥ $50/41d vs baseline.

| Sym | Current $ | Best variant | New $ | Δ | Action |
|---|---:|---|---:|---:|---|
| HYPE | −$29 | ema_cross | +$70 | +$99 | **DEPLOYED** |
| ETH | −$79 | sjm | +$55 | +$133 | **DEPLOYED** |
| ARB | −$100 | both_agree | +$56 | +$156 | **DEPLOYED** |
| ZEC | **+$630** | sjm (tied) | +$630 | $0 | hold — already optimal |
| ENA | +$470 | sjm | +$490 | +$19 | hold — gain too small |
| PENDLE | +$67 | structure (tied) | +$68 | +$1 | hold |
| TIA | +$216 | (no variant beats) | +$216 | $0 | hold — already optimal |
| OP | data error | — | — | — | deferred (HL 1h 2000-bar fetch issue) |

**Total expected uplift from deploys: +$388 / 41d** (HYPE +$99, ETH +$133, ARB +$156). All three convert net-loser-or-near-zero baselines into net-winners.

### Surprising findings

1. **The "filter ON is better" pattern doesn't generalize.** Four symbols (ZEC, ENA, PENDLE, TIA) are quietly running with `use_1h_filter: false` and that's the optimal setting for them — every filter variant either ties or hurts.
2. **The wiki's documented per-symbol filter assignment was largely fiction.** It claimed HYPE/FARTCOIN/SOL → SJM, but FARTCOIN/SOL aren't trading and HYPE was running filter-OFF AND would have been worst-on-SJM.
3. **Per-symbol best filters:** BTC → sjm, HYPE → ema_cross, ETH → sjm, ARB → both_agree, XRP → hma_slope (unchanged from 04-21). No single filter dominates.

### Methodology used

For each symbol:
1. Build `Cfg` from deployed config (preserves entry_type, exit_type, TP ladder, etc.).
2. Run baseline backtest with deployed `use_1h_filter` value.
3. Run 5 variants with `use_1h_filter=True` and `trend_filter_1h ∈ {ema_cross, structure, both_agree, hma_slope, sjm}`.
4. Compute net P&L per variant, deploy best variant if Δ ≥ $50/41d vs baseline.

Output: per-symbol notes inline above. Bot restart needed for changes to take effect.

---

## #6 — Pullback strategy generalization

**Status:** open
**Opened:** 2026-04-26
**Origin:** Per-strategy stats analysis (51 trades, 2026-04-21 → 2026-04-26).

**Observation.** The two `pullback_in_regime` symbols (HYPE, ZEC) are **carrying the entire bot**:

| Strategy | N | WR | Net | PF | Sharpe |
|---|---:|---:|---:|---:|---:|
| **pullback_in_regime** (HYPE, ZEC) | 14 | 64.3% | **+$916** | **5.46** | **0.40** |
| ensemble_regime (7 syms) | 26 | 61.5% | −$119 | 0.86 | −0.04 |

Same ~62-64% WR, opposite outcomes. The asymmetry sits in the **avg win / avg loss ratio**:
- Pullback: $125 win / $41 loss = **3:1 favorable**
- Ensemble: $44 win / $82 loss = **1.87:1 inverted**

**Hypothesis.** `pullback_in_regime`'s exit design (`pullback_exit` on opposite-pivot or regime flip) captures multi-ATR moves that the ensemble's TP-ladder partials cap. Combined with the regime gate (no trades in chop by construction), pullback may have a more durable edge than ensemble — especially in the current regime.

**Confound.** Today's `use_1h_filter` audit (seed #5) showed all ensemble symbols were running with the 1h filter OFF. The +$388/41d expected uplift from those swaps will partly close the gap. So we can't fairly compare ensemble-vs-pullback until the post-swap data lands.

**Test plan.**
1. **Wait until 2026-04-30** for the filter-swap comparison ([decision doc](../decisions/2026-04-26_filter_swap_baseline_snapshot.md)).
2. If ensemble's per-trade expectancy is still negative or below pullback's even after the swaps, run a **pullback grid backtest** on the ensemble symbols (BTC, ETH, ARB, ENA, OP, PENDLE, TIA). Use `research/whale_oos.py` with `entry_type=pullback_in_regime, exit_type=pullback_exit` instead of the deployed ensemble configs.
3. Pass criteria same as other deployments: PF ≥ 1.2, WR ≥ 40%, n ≥ 15, beats random by ≥ 0.5 PF, ±20% sensitivity holds.
4. For each symbol that passes pullback OOS with a higher net than its current ensemble config, flag for swap.

**Risk to watch.** Pullback fires less often (only on validated pivots). If the ensemble symbols rarely produce pivots at the right times, the pullback config might silently produce n=0 — a kill-switch outcome, not edge. INJ's failed cohort OOS showed exactly this pattern (config never triggered).

**Don't confuse with seed #5.** Seed #5 was about activating the existing 1h filter. This seed is about replacing the entire entry/exit framework. Order matters: complete #5's evaluation first.

---

## How to file a new seed

Append a new `## #N — <title>` block. Keep the format: status, opened date, origin, observation, hypothesis, candidate tests / next test, open questions. Promote to `decisions/` (with the seed `Status: closed → see decisions/...`) once tested.
