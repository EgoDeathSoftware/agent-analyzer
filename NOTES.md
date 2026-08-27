# sessionkit — notes

Research, borrowed ideas, and history. Nothing here is a commitment: `PLAN.md` owns scope and
`SPEC.md` owns decisions. This file exists so those two can stay short.

**Contents:** [Prior art](#1-prior-art) · [Ideas worth stealing](#2-ideas-worth-stealing) ·
[Changelog](#3-changelog) · [Superseded reasoning](#4-superseded-reasoning)

---

## 1. Prior art

Surveyed 2026-08-26. This section exists because the first draft of the skill list was written
without it, and three of the proposed skills turned out to have crowded competition.

| Tool | Does | Bearing on scope |
|---|---|---|
| [`ccusage`](https://github.com/ryoppippi/ccusage) (~4.8k ★) | Cost/token reports over the same JSONL: daily/weekly/monthly/session, **5-hour billing windows**, statusline, LiteLLM-synced pricing | Basic cost reporting is solved. `cost-forensics` must differentiate on **bloat attribution**, not rebuild tables. Two features we lack outright — see below |
| [`agent-retro`](https://github.com/giannimassi/agent-retro) | End-of-session retro: conversation arc, per-agent token cost, tool-result waste detection, friction analysis tracing corrections to root causes | Overlaps `session-forensics` and part of `cost-forensics` — **single-session scope** |
| [`claude-improve`](https://github.com/TerenceBristol/claude-improve) | Config audit across CLAUDE.md/skills/agents/settings, plus a **learning loop** in `~/.claude/improve-learnings.md` that re-ranks findings by what the user previously accepted or rejected | Overlaps `suggest-skills`. The learning loop is an idea worth stealing — §2.3 |
| bitwarden `claude-retrospective`, `accidentalrebel/session-retrospective`, Zhutov's `/retrospective` | Variations on single-session retro → markdown | Confirms the pattern is commodity |
| [`claude-code-log`](https://github.com/daaain/claude-code-log), [`claude-code-transcripts`](https://github.com/simonw/claude-code-transcripts), claude-devtools, the Rust parser crate | JSONL → HTML/Markdown; subagent debugging; compaction diagnosis; a typed parser with a **round-trip schema-drift validator** | Rendering is solved. The drift validator is the pattern the parity test should copy |

### The differentiator is corpus scope

Every retrospective tool above analyses *one* session, usually the current one. `ccusage`
aggregates across sessions but only over cost. **Nothing found clusters failures across a whole
corpus.** That is exactly what `error-patterns` already does — 269 failed calls across 87
transcripts collapsing to one hook rule responsible for 68 of them — and no single-session tool
can reach that finding by construction.

Consequences, all of them now applied in `PLAN.md`:

- **Corpus-scoped skills have no competition** — `error-patterns`, `edit-churn`, `suggest-skills`,
  `unfinished-work`. Double down there.
- **Single-session skills are commodity.** A plain `session-digest` competes with five free
  alternatives; it survives only as the writer for the tracker UI's summary pane, an integration
  no external tool has. Any ambition of it being a general retro is dropped.
- **`session-forensics` must stay narrow.** `agent-retro` covers the general "what went wrong"
  retro. Ours earns its place only on mechanical failure modes it can prove from typed system
  events — `agents_killed`, tool loops via `input_digest`, stalls, compaction churn — and by
  ending in a prevention diff rather than a narrative.

### Two `ccusage` features we lack

1. **5-hour billing windows.** Anthropic bills in 5-hour blocks; both pricing tables report
   per-session sums, which do not line up with what the user is actually charged. A correctness
   gap in the tracker UI as much as in `sk cost`.
2. **LiteLLM-synced pricing.** The same rates are hand-maintained in `pricing.py` and the
   tracker's `server/src/pricing.ts` — a table that was already wrong once (Opus at 3× for an
   unknown period). A generated table from a maintained upstream removes that whole bug class.
   Conflicts with the no-network rule unless the sync is a committed artifact refreshed
   deliberately rather than a runtime fetch. Tracked as an open decision in `PLAN.md`.

---

## 2. Ideas worth stealing

Concrete mechanisms from §1, read from source rather than summaries. Attribution is kept so the
origin of each heuristic stays traceable — a threshold someone else validated against their
corpus is worth more than one we invent, and worth less than one we re-validate against ours.

### 2.1 Detection heuristics

`session-forensics` and `cost-forensics` have detectors but almost no calibrated thresholds.
These are the missing numbers:

| Signal | Heuristic | From |
|---|---|---|
| Wasted delegation | agent dispatch costing **> $1** whose result was discarded or only partly used | agent-retro |
| Oversized tool results | a tool averaging **> 10 KB** per result that wasn't meaningfully used → suggest `offset`/`limit` or an Explore agent | agent-retro |
| Premature commitment | **same tool 3+ times sequentially** — was the first attempt a guess? | agent-retro |
| Abandoned approach | **5+ tool calls followed by a completely different approach** | agent-retro |
| Session past its useful life | **500+ turns**, **4h+ duration**, or **$300+ cost** → recommend handoff | agent-retro |
| Under-specification | Claude asked a clarifying question answerable from files already in context | agent-retro |

Every one of these must be **re-validated against this corpus and the divergence recorded** before
shipping. Ours to add, since it needs the corpus: the same threshold crossed *repeatedly across
sessions* is a different finding from one session hitting it once. That distinction is the moat.

### 2.2 A friction lexicon

For `suggest-skills`, which was specified with no detection method at all. agent-retro classifies
user messages as:

- **corrections** — "no", "not that", "wrong"
- **redirects** — "instead do X", "let's try a different approach"
- **repetitions** — "I already said", "like I mentioned"
- **stops** — "wait", "hold on", "undo"
- **frustration** — terse responses following prior engagement

…then traces each through a fixed causal chain: *user correction → what did Claude do wrong → why
did it do that → wrong assumption / missing context / bad skill guidance / wrong tool?* Adopt the
chain as the required output shape, so findings land on a cause rather than a complaint.

The **repetitions** class is the load-bearing one: a thing the user says twice is a missing rule;
a thing they say across five sessions is a missing skill. That single sentence is why
`prompt-autopsy`, `corrections-to-rules` and `suggest-skills` are one skill and not three.

### 2.3 A self-pruning feedback loop

`claude-improve` keeps learnings at two tiers — global `~/.claude/improve-learnings.md` for
cross-project preferences, per-project for distilled patterns, **deferral counters**, and
date-keyed run logs. The key rule: **patterns confirmed across ~5 runs get promoted into real
config and deleted from the learnings file**, so it never grows without bound.

agent-retro reaches the same end with less machinery: an actions table whose status moves
`proposed → done | deferred | rejected`.

Prefer agent-retro's simplicity; steal claude-improve's promotion rule, which is what stops a
learnings file becoming another unread log. Open decision in `PLAN.md`, because it would be
sessionkit's only persistent writable state.

### 2.4 Output-contract refinements

The contract enforces a byte budget by cutting rows. These fit more signal into the same bytes
instead:

- **Detail ladder** `user-only → full`, and a **compact mode that merges consecutive same-type
  sections**, described by its author as being for "feeding past conversations to an LLM" — which
  is precisely our budget problem, solved by a rendering flag rather than truncation.
  *(claude-code-log)*
- **`conversation_arc`** — user messages plus assistant text responses only, tool noise dropped,
  as the narrative spine of a session. A better default for `sk show` than a raw timeline.
  *(agent-retro)*
- **`--metadata-only`, reading just the first and last 64 KB** to identify a session without
  parsing it. Cheap win for `sk index` on multi-MB transcripts. *(agent-retro)*
- **Natural-language dates** — "today", "yesterday", "last week" alongside `--since 1d`.
  *(claude-code-log)*
- **`file:line` citations required on every finding**, and **successes reported alongside
  problems** so a report is not pure negativity. *(bitwarden)*

### 2.5 Cross-source corroboration

bitwarden's retro joins the transcript against `git log --since` over the session window, plus
compile/test status of the changed files. The git join is planned for `unfinished-work`; it
belongs in `session-forensics` too — *"the session claimed success, produced no commit, and left
tests failing"* is a finding no transcript-only tool can make.

It also **interviews participating subagents** ("what worked well for you / how could coordination
be improved"). Not available to us post-hoc, but the transcript-derived analogue is comparing a
subagent's returned summary against what the parent actually used — which is the delegation
section of `cost-forensics`.

### 2.6 Data preservation, and a gap it exposes

claude-code-log ships a **PreCompact hook that auto-archives transcripts and subagent logs
immediately before compaction**. Worth stealing on its own terms — but it also raises a question
never asked: *what does compaction destroy that we are trying to analyse?*

The `compaction-churn` detector reads what survived. If pre-compaction content is unrecoverable
from the transcript, an archive hook is a **prerequisite for that detector being honest**, not an
optional extra. Open decision in `PLAN.md`; it is a ten-minute check against a compacted session.

### 2.7 Live surfaces, not just post-hoc reports

`ccusage` ships a **statusline** integration; claude-devtools **tails running sessions in real
time**. Everything in `PLAN.md` is retrospective. A statusline that warns while a session is still
cheap to correct — approaching a 5-hour block limit, or past the 500-turn handoff threshold — is
worth more than a report written afterwards.

Note the tracker already owns the live half of this (SSE-backed session status), so this may
belong in the UI rather than the CLI.

### 2.8 Dashboard signals worth reporting without a dashboard

`claude-code-otel` monitors, among others: tool success rate, tool permission decisions, error
rate by model, API latency by model, cache-read vs. creation ratio, and lines added/removed per
session. `sk digest` should cover the same ground in text. **Lines-changed per session** is a
productivity denominator we have no equivalent for and can compute from `file-history/`.

---

## 3. Changelog

### 2026-08-26 — pricing corrected on both sides

`server/src/pricing.ts` in the tracker was stale: it billed Opus at Opus-4.5-era $15/$75 against
an actual $5/$25 — a **3× overcharge on every Opus session** — and had no Claude 5 entries at all,
so `claude-opus-5` fell through to the Sonnet default and was *under*-charged.

Ported from `sessionkit/pricing.py` so the two agree by construction. Added ID normalization
(`[1m]`/`-fast` suffixes, `anthropic.` prefix, dated snapshots), made `<synthetic>` non-billable,
and added `getUnpricedModels()` so unknown models are recorded rather than silently defaulted.

**Corpus total moved $604.59 → $416.05.** 10 new tests; tracker suite at 216.

### 2026-08-26 — sessionkit's SQLite cache removed

`cache.py` (211 lines) and `ingest.py` (150) deleted along with the `sk ingest` command, the
`--no-refresh` flag, and `~/.cache/sessionkit/cache.db`. **No module in sessionkit imports
`sqlite3` any more.**

Measurement killed it — see `SPEC.md` §2 for the figures. The cache bought nothing and cost an
ingest step, a schema, a staleness contract, and ~360 lines. It also added friction that bit
during Phase 1: a taxonomy change required a full re-ingest before `sk show` reflected it.

**This was a query-layer rewrite, not a deletion.** An earlier revision of the spec called it "a
pure simplification", which was wrong. SQLite was not a speed layer bolted on: it was the query
engine, with **16 SQL statements across 11 handlers** in `cli.py`, and the schema in `cache.py`
serving as the data model. Two modules replaced it:

- **`corpus.py`** — discovery, the on-demand parse, and the post-parse pipeline that was buried in
  `_ingest_one`. `parse_file` alone returns `end_state="unknown"` with no error classes and no
  anomalies; `load_one` is what makes a session usable.
- **`query.py`** — the aggregations that were SQL. Each returns plain dicts keyed as the old
  `SELECT` aliases were, so the rendering code was left untouched.

**Verified by golden diff**, because the CLI had *no* test coverage: all 20 surfaces captured
against a frozen fixture corpus before and after. 13 byte-identical; 7 differed only in tie
ordering and in `doctor` dropping its cache path. No content changed. Suite 87 → 96 — two
cache-mechanics tests deleted, eleven added for the query layer and the `show` fast path.

**Measured after:** whole-corpus commands ~1.1 s (a full parse; the cache made them ~0.3 s).
`sk show` resolves by filename and parses one file — ~0.25 s, down from ~1.1 s, with a
verify-then-fall-back guard, because filename-equals-session-id is a convention rather than a
guarantee. The fast path can only ever confirm a hit, never rule one out.

**One deliberate behaviour change:** equal-count rows in `sk errors` and the `sk show` tool table
now tie-break alphabetically. The old `ORDER BY n DESC` left ties in SQLite's sorter order, which
against this corpus matched neither insertion nor alphabetical order — reproducible only by
accident.

It was a correctness fix too: `cache.py` held `open_tracker()`, which opened a `tracker.db` that
no longer exists.

### 2026-08-26 — the tracker's database removed

`db.ts`, `llm.ts`, `llm-config.ts` and `auto-summarize.ts` deleted (661 lines), along with the
tags and prompts features on both sides.

| Table | Rows it ever held | Outcome |
|---|---:|---|
| `tags`, `session_tags` | 0 | Feature deleted — routes, `useTags.ts`, `TagPills.tsx`, the sidebar filter |
| `prompts` | 0 | Feature deleted — routes, `usePrompts.ts`, `PromptLibrary.tsx` |
| `session_summaries` | 0 | Replaced by files |
| `session_fts` | 81 | Replaced by an in-memory scan |

The argument for keeping the database was to protect "data that cannot be regenerated from
transcripts" — tags, prompts, cached summaries. **That data never existed.** Every annotation
table held 0 rows: in the tracker's whole operating life nobody created a tag, saved a prompt, or
generated a summary. The schema was waiting for data that never arrived.

**Search** is now a substring scan over the registry's already-resident `Session` objects. The
index it replaced covered 542 KB of a 42 MB corpus — text blocks only, capped at 50 KB per
session, subagents excluded — so the scan is *more* complete as well as index-free.

**Summaries** are written host-side as `<SUMMARY_DIR>/<session-id>.md` by a skill. The server only
reads them: `GET /api/sessions/:id/summary` returns the file body plus its mtime, and
`SUMMARY_DIR` is mounted `:ro` into the container. Because the writer is host-side, the
named-volume-versus-bind-mount problem the spec once called a prerequisite never arises — the
`tracker-data` volume is gone entirely. `AiSummary` narrowed to `{ content, generatedAt }`; there
is no `model`/`provider` metadata to record once the server isn't the generator. Staleness is now
`lastActivityAt > generatedAt`.

> **A correction worth keeping.** An earlier revision proposed deleting summaries outright,
> reasoning from `session_summaries` having held 0 rows — the same evidence that killed tags and
> prompts. That was wrong. Tags and prompts had no consumer; a summary has one, the session detail
> pane of a UI in daily use. Zero rows there measured a missing **writer**, not missing demand.

**Verification:** server and client both typecheck clean; 203 tests across 16 files pass (21
deleted with their modules, 8 added for the two replacement routes). The path-traversal guard on
the summary route was mutation-tested — removing the guard fails the test.

**Not removed:** `better-sqlite3` stays a dependency. It reads opencode's *own* database, which is
unrelated to `tracker.db`.

**An existing exposure, now closed:** `session_fts.content` had stored up to 50 KB of unredacted
transcript text per session since the index shipped — a second unencrypted copy of transcript
content, in a file with a different lifetime and different backup exposure from the originals.
Deleting the index removes that copy.

**Cleanup still outstanding:** the stale `~/.claude/tracker/tracker.db` on the host is orphaned
and still holds that text. Delete it — nothing reads it any more.

### 2026-08-26 — taxonomy sharpened, first pass

Added `timeout` and `killed` (from exit codes 124/137/143), `input-validation`, and
`policy-denied`. Unclassified across the corpus went **14 → 7**.

This is a standing loop, not a step that completes: run `sk show <id> --mode errors` on a real
transcript, find where the diagnosis is wrong or unhelpful, fix `classify.py`, re-run.

### 2026-08-26 — an arithmetic correction

The first draft of the plan claimed *"hook-block ranks #1 at ~47"*. That number came from
top-level files only, and compared a merged `hook-block` class against *unmerged* `exit-code`
prefixes. With subagents included and both properly merged, hook-block is **#1 at 73**.

The conclusion held; the arithmetic behind it did not. This is the same class of bug as the
top-level-only glob that silently dropped 41% of the corpus, and it is the reason `PLAN.md` argues
the parser has to be shared and tested rather than improvised per skill.

---

## 4. Superseded reasoning

Resolved while a telemetry store was still the plan. Recorded because the reasoning is reusable if
the ~10× corpus threshold in `SPEC.md` is ever crossed.

- **Engine: SQLite.** Decided by the reader — sessionkit is stdlib-Python-only by charter, and
  `sqlite3` with FTS5 is in the stdlib (verified: host Python, SQLite 3.45.1, FTS5 compiled in).
  DuckDB needs pip and admits one read-write process at a time; Postgres makes analysis depend on
  a running server. Low-regret either way: DuckDB reads a SQLite file directly via
  `sqlite_scanner`, so the read engine stays a separate, later, reversible choice.
- **One file, not a separate `telemetry.db`.** Settled by a constraint, not a preference: FTS5
  external content resolves its content table in the FTS table's *own* database —
  `content='tel.messages'` across an `ATTACH` fails with `no such table` (verified). Splitting
  would have dragged the FTS index away from `tags`. `telemetry.db` never existed.
- **Retention: unbounded.** Telemetry would have been bounded by what is in `~/.claude/projects`;
  the missing piece was never a policy but calling `removeSession` when a transcript disappears.
- **Schema details** — `ON DELETE CASCADE` on every child table, `(session_id, idx)` primary keys,
  partial indexes (`WHERE is_error = 1`) over full-column ones, `tool_outputs` split from
  `tool_calls` to keep the aggregate-scan table narrow, and triggers to keep an external-content
  FTS index in sync. See git history for the full DDL.

**Also not built, and for the same reason:** telemetry tables, `storeTelemetry`, `meta(k,v)`, the
version/staleness contract, FTS over `messages`, the compose bind-mount and container-uid changes,
and a sessionkit CLI rewrite against the store. All of them existed to serve the store.
