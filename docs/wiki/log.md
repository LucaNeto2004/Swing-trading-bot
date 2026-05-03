# Wiki Log

Append-only chronological record. Never edit existing entries — append new ones.

Format: `YYYY-MM-DD — <type>: <one-line summary>` followed by optional sub-bullets.

Types: `decision`, `ingest`, `deploy`, `incident`, `lint`.

---

2026-04-12 — decision: GARCH-based position sizing evaluated and rejected. See `decisions/2026-04-12_garch_rejected.md`.

2026-04-16 — decision: Pivot from momentum (commodities-bot + crypto-bot) to whale swing strategy. New repo `swing-trading-bot/`. See `decisions/2026-04-16_whale_pivot.md`.

2026-04-19 — decision: `xyz:SILVER` restricted to weekdays 08:00–22:00 UTC after Saturday 2026-04-18 triple-SL event. See `symbols/xyz_SILVER.md`.

2026-04-19 — decision: ZEC added to pyramid banned list after backtest showed pyramid costs $681 / PF 2.24→1.65. Lives in `config/settings.py::PyramidConfig.banned_symbols`.

2026-04-20 — deploy: Commission bumped 0.00006 → 0.00030 in `config/settings.py::RiskConfig.commission_pct`. HL tier-0 crypto perp, 50/50 maker/taker realistic.

2026-04-21 — decision: Per-symbol `trend_filter_1h` deployed after 41-day head-to-head OOS (baseline −$1,103 → best-of-three +$442). See `decisions/2026-04-21_regime_filter_swap.md`.

2026-04-22 — deploy: `max_concurrent_positions` raised 2→4 (total cap, any mix). Group sub-caps (`max_crypto_concurrent`, `max_commodity_concurrent`) set equal to total so only the total binds. See `concepts/risk-gate.md`.

2026-04-23 — lint: Risk-gate posture re-verified against `config/settings.py::RiskConfig`. All fields match wiki claims as of this date.

2026-04-24 — ingest: Wiki bootstrapped. Seeded concepts (risk-gate, sizing, regime-filters), decisions (garch, whale-pivot, regime-filter-swap), one symbol (xyz:SILVER). Other symbols pending.

2026-04-24 — deploy: `core/journal.py` added; wired into `main.py::_run_cycle` — UTC day-roll writes `logs/daily/YYYY-MM-DD.md` automatically. Backfilled 2026-04-22/23/24 from `data/paper_state.json`.

2026-04-24 — deploy: CLAUDE.md drift fixed — `max_concurrent_positions` 2→4, symbol list refreshed to current deployed set (16 symbols), risk-gate section annotated with paper/live split + wiki pointer.

2026-04-24 — ingest: WIKI.md updated — `logs/daily/` added as primary lint ingest target; lint report format added.

2026-04-26 — ingest: New `concepts/research-seeds.md` opened with seeds #1 (compression / chop filter — origin Sun 04-26 −$276 day, BTC-1h-confirm hypothesis falsified) and #2 (weekend regime treatment — Sat+Sun both losing).

2026-04-26 — ingest: Seed #2 backed by data — 14d BTC weekday avg daily range 3.47% vs weekend 1.99% (0.57×); this weekend specifically 0.97% (Sat) / 1.15% (Sun) — ~30% of even the weekend average, so this weekend is a tail compression event on top of the structural weekend effect.

2026-04-26 — ingest: Seeds #3 (0% WR cohort — LINK, SOL, xyz:CL, OP, INJ, TIA: 13 trades / 0 wins / −$620 over 6d, OOS gate prerequisite logged, no action without it) and #4 (LONG/SHORT asymmetry — shorts +$273 / longs −$10 over same window, parked until N ≥ 60/side).

2026-04-26 — decision: HOLD — no config or deployment changes. Awaiting more data and the OOS gate (seed #3) before any benching. Will reassess after Sunday EOD journal auto-writes (00:00 UTC 2026-04-27).

2026-04-26 — research: Cohort OOS executed via `research/whale_oos.py` (LIVE_SYMBOLS overridden). 4 of 6 testable: LINK, SOL, INJ, TIA — **all FAIL OOS**. OP excluded (HL 1h fetch returns empty for 2000-bar request, 15m/4h fine). xyz:CL excluded (missing from `config/settings.py::INSTRUMENTS`). Output: `/tmp/whale_oos.json`. Detailed write-up: Obsidian `research/2026-04-26_cohort_oos.md`. Bench action awaiting Luca's go.

2026-04-26 — deploy: Bench applied — `whale_LINK.json`, `whale_SOL.json`, `whale_INJ.json` moved to `config/deployed/_retired/`. TIA kept against OOS verdict per Luca call. Universe 16 → 13 active. Seed #3 closed → `decisions/2026-04-26_cohort_bench_and_btc_sjm.md`.

2026-04-26 — deploy: BTC config swapped to SJM 1h filter — `whale_BTC.json`: `use_1h_filter false→true`, `trend_filter_1h ema_cross→sjm`. 41d head-to-head: filter-OFF baseline −$231 → SJM +$79 (+$310 delta), WR 66.7%→79.2%, PF 0.79→1.19, entries cut 31→13. SJM was the only filter that actually worked on BTC (HMA was a no-op, others were worse). BTC was the last filter-OFF symbol — now everything in the universe runs a 1h filter. Bot restart needed (Luca handles).

2026-04-26 — ingest: `concepts/regime-filters.md` updated to reflect post-bench, post-BTC-swap per-symbol filter assignment.

2026-04-26 — deploy: Dashboard upgraded to show per-asset 1h filter (use_1h_filter + trend_filter_1h), TP ladder (TP1/TP2/TP3 percentages), trail and SL multiples. `dashboard.py` symbol_cards extended; `templates/dashboard.html` renders new fields. Dashboard restarted to load new INSTRUMENTS (10 symbols, no LINK/SOL/INJ) + BTC SJM swap.

2026-04-26 — incident: Dashboard surfaced that 8 of 10 active symbols have `use_1h_filter: false` despite having `trend_filter_1h` set. The 2026-04-21 regime filter swap may not have actually deployed for any symbol other than (now) BTC and (already) XRP. Filed as seed #5 in `concepts/research-seeds.md`.

2026-04-26 — research: HYPE 41d head-to-head — current (filter OFF) −$29 → ema_cross +$70 (+$99 delta). SJM is **worst** for HYPE (−$166). Confirms wiki/memory that "HYPE → SJM" assignment was fiction. Proposed config edit pending Luca's go.

2026-04-26 — deploy: `config/settings.py::INSTRUMENTS` cleaned — SOL/LINK/INJ commented out with retirement context (mirrors existing LIT/xyz:CL pattern). Eliminates "no deployed config" warnings at startup.

2026-04-26 — deploy: HYPE 1h filter activated — `whale_HYPE.json`: `use_1h_filter false→true` (trend_filter_1h was already `ema_cross`, just needed activation). 41d head-to-head: −$29 baseline → +$70 (+$99 delta). SJM was the WORST variant for HYPE (−$166), contradicting wiki memory that HYPE was on SJM.

2026-04-26 — research: Full universe `use_1h_filter` audit completed (seed #5). Tested ZEC/ENA/ETH/ARB/PENDLE/TIA — 2 deploy candidates (ETH→sjm +$133, ARB→both_agree +$156), 4 holds (ZEC/ENA/PENDLE/TIA: current already optimal). OP errored (HL 1h fetch issue, same as cohort OOS). Total expected uplift: +$388/41d across HYPE+ETH+ARB.

2026-04-26 — deploy: ETH SJM filter activated — `whale_ETH.json`: `use_1h_filter false→true`, `trend_filter_1h ema_cross→sjm`. 41d: −$79 → +$55 (+$133 delta).

2026-04-26 — deploy: ARB both_agree filter activated — `whale_ARB.json`: `use_1h_filter false→true`, `trend_filter_1h ema_cross→both_agree`. 41d: −$100 → +$56 (+$156 delta).

2026-04-26 — ingest: `concepts/regime-filters.md` updated again — per-symbol assignment now reflects the verified post-audit state. Seed #5 closed.

2026-04-26 — ingest: Per-strategy stats analysis (51 trades) — pullback_in_regime PF 5.46 / +$916 / Sharpe 0.40 (HYPE+ZEC) vs ensemble_regime PF 0.86 / −$119 / Sharpe −0.04 (7 syms). Same WR (~62%) but inverted avg-win/avg-loss ratios (3:1 vs 1:1.87). Filed as seed #6 (pullback generalization).

2026-04-26 — ingest: Baseline snapshot saved for the 4 filter swaps (BTC/HYPE/ETH/ARB) at `decisions/2026-04-26_filter_swap_baseline_snapshot.md`. **Action queued: compare predicted vs realized on 2026-04-30 (Thursday).** Pre-change configs preserved in the doc for clean revert if needed.

2026-04-27 — ingest: Sunday EOD review (auto-journal `logs/daily/2026-04-26.md` fired clean at 00:00 UTC). Weekend total: **−$341.58 / 18 trades** (Sat −$108 / 3, Sun −$234 / 15). Monday already **+$335 / 10 trades** (early UTC) — recovery in progress. Validates seed #2 (weekend regime). Key Sunday signals:
  - **BTC −$113 SL at 08:22 UTC, FE=0.33 ATR** — the SJM-vetoable failure mode, exactly. Loss happened ~12h BEFORE the SJM swap deployed (~20:30 UTC). Validates the BTC change.
  - **OP −$65 across 2 SL hits**, both FE < 1 ATR — same vetoable pattern. OP is the one symbol with broken HL 1h fetch (can't apply a filter yet). Bumps the OP fetch fix in priority.
  - **LINK −$70 at 17:00 UTC** — last loss before bench was applied. Vindicates the 04-26 cohort retirement.
  - **Runner-stop giving back gains**: ZEC −$27 (FE=7.57 ATR), ENA −$9 short (FE=13.98 ATR). Two trades that ran 7×–14× ATR favorable then trailed back to a runner_stop loss. Possible runner-trail tuning issue — note for later, not actioning now (n=2).
  - **TIA "kept against OOS verdict" still in observation**: Sun −$37 (choch_exit) → Mon +$15 (tp1_partial). No verdict yet.
