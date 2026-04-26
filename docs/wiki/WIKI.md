# Swing Bot Wiki — Conventions

Karpathy-style LLM Wiki for the swing trading bot. Canonical, portable, editable by any LLM agent working in this repo.

## Three layers

1. **Raw sources** (immutable) — `config/deployed/*.json`, `research/*.py`, `research/*.ipynb`, `logs/`, `data/paper_state.json`. Read-only from the wiki's perspective. **`logs/daily/YYYY-MM-DD.md`** is auto-written by the bot at UTC day-roll (see `core/journal.py`, hooked from `main.py::_run_cycle`) — these are the primary ingest target for weekly lint.
2. **Wiki** (LLM-owned markdown) — this directory. Summaries, entity pages, concept pages, decisions. Keep current with the raw layer.
3. **Schema** — `/CLAUDE.md` at repo root + this file. Defines conventions and workflows.

## Directory layout

```
docs/wiki/
├── WIKI.md              — conventions (this file)
├── index.md             — catalog of every wiki page (content-oriented)
├── log.md               — append-only chronological record
├── concepts/            — durable topics (sizing, risk-gate, regime-filters, …)
├── decisions/           — dated one-shot calls (YYYY-MM-DD_<slug>.md)
└── symbols/             — per-symbol pages (BTC.md, xyz_SILVER.md, …)
```

## Page anatomy

Every page opens with:

```
# <title>

**Status:** active | superseded | archived
**Last verified:** YYYY-MM-DD
**Sources:** <file paths or URLs the page summarizes>

<body>
```

`Last verified` is when someone last checked the page matches reality. `Sources` is where to re-verify.

## Operations

### Ingest
Triggered when: a deployed config changes, a research result lands, a decision is made, or a memory moves from Claude Code auto-memory into the repo.

Steps:
1. Write/update the relevant page(s).
2. Update `index.md` if a page is new or the one-liner changed.
3. Append to `log.md` with the date prefix (`YYYY-MM-DD — <summary>`).
4. Cross-link: if the new page supersedes another, set the old one's status to `superseded` and link forward.

### Query
When answering a question about the bot:
1. Read `index.md` first to find relevant pages.
2. Read only those pages; don't grep the raw layer unless the wiki is silent or stale.
3. If the wiki conflicts with the raw layer, trust the raw layer and update the wiki.

### Lint
Weekly (user-triggered with "run the wiki lint" or similar):

1. **Walk every wiki page.** Check `Last verified` against today. Anything >30 days old → re-verify claims against the `Sources` listed on the page, bump the date or edit the page.
2. **Scan `logs/daily/*.md`** since the last lint. Surface notable events (unusual P&L, new regime patterns, new exit reasons, halted symbols). Promote anything that changes a durable claim into the relevant concept/symbol/decision page. Fill the Notes section on each daily journal with a one-liner if there was something worth remembering.
3. **Flag contradictions** between pages. If two pages disagree, check the raw layer and update the stale one.
4. **Flag orphans** (pages not in `index.md`) and **missing pages** (concepts referenced but undocumented).
5. **Append a report to `log.md`** using the format below.

#### Lint report format

Append an entry per lint pass to `log.md`:

```
YYYY-MM-DD — lint:
- Verified: <pages re-dated>
- Updated: <pages edited with what changed>
- Promoted from daily: <dates → target pages>
- Contradictions: <found/none>
- Orphans: <found/none>
- Missing: <concepts referenced but not documented>
```

## Hard rules

- Never edit pages in `decisions/` after the decision date — supersede with a new dated doc instead.
- Never delete from `log.md` — append-only.
- If a claim cites a file:line, verify it still exists before writing. File paths rot.
- Keep pages terse. A wiki page is a summary, not a tutorial.
