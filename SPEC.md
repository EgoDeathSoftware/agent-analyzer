# Decision record: the filesystem is the store of record

**Date:** 2026-08-26 · **Status:** Decided and implemented.

This document answers **why sessionkit is built the way it is**. It does not say what is being
built — `PLAN.md` owns that — and it does not carry history, which lives in `NOTES.md`.

**Scope:** `kind: claude-code` sources, and both surfaces over them: the tracker UI (in daily use)
and sessionkit, its command-line counterpart. opencode is explicitly *not* covered — see Roadmap.

**Relates to:** `claude-project-tracker/docs/superpowers/specs/2026-08-21-container-session-ingestion-design.md`
(sibling repo).

---

## 1. The decision

**Do not build a telemetry store, and do not keep a database.** The JSONL corpus in
`~/.claude/projects/**` is the store of record. Anything derivable from a transcript is derived on
demand; anything generated is written as a file by whatever generated it.

An earlier draft of this document proposed the opposite: persisting parsed session telemetry into
`tracker.db` and rewriting sessionkit to query it. **That design is rejected** — the measurements
below show the store would be slower than the thing it replaced. Both databases have since been
removed; see `NOTES.md` §3 for what that took.

## 2. Why — the measurements

Every number here is from the current corpus: **42 MB of JSONL across 87 transcripts** (51
top-level, 36 subagents) on one host. This section is the authority for these figures; other
documents reference them rather than restating them.

| Operation | Without a database | With one |
|---|---:|---|
| Full-corpus text search | **7 ms** (`rg` over all 87 files) | FTS5 index, trigger-maintained, rebuilt on schema bump |
| One session parsed cold, including anomaly detection | **172 ms** median | **180 ms** median reading a `cache.db` |
| Whole corpus re-parsed from scratch | **1.2 s** (~34 MB/s) | 1.2 s to *build* the same thing |

Two of these are decisive.

**The cache was not faster than parsing.** 172 ms cold versus 180 ms cached, five runs each, same
session. Both are dominated by ~100 ms of Python interpreter startup; the actual parse of a 3.7 MB
transcript is roughly 70 ms. A 4.8 MB `cache.db` bought nothing and cost an ingest step, a schema,
a staleness contract, and ~360 lines.

**Full-corpus search is 7 ms.** The external-content FTS5 table this document previously
specified — with its trigger-maintenance requirement and its silent-desync failure mode — existed
to beat seven milliseconds.

> The error-clustering figures quoted elsewhere (269 failures across 86 transcripts) come from an
> earlier run against 86 files. The 87th transcript postdates it; the conclusions are unaffected.

### 2.1 Why the filesystem is an unusually good store here

Session JSONL files are **append-only**: a file grows, but past lines never change. That removes
the two problems databases exist to solve. Cache invalidation collapses to `(mtime, size)`. There
is no mutation, no concurrent-writer coordination, and no consistency window. If parsing ever does
get slow, the same property makes incremental parse-from-byte-offset trivial.

An earlier draft framed the tracker registry's in-memory `Map` as data loss — *"it throws them
away on restart"*. Nothing is thrown away. The transcripts are still on disk, and the map rebuilds
from them in about a second.

### 2.2 What would change the decision

**Corpus-wide analysis is in scope** — `sk errors`, `sk cost` and `sk digest` all sweep every
transcript, and that is the point of them. The decision survives that because a full sweep is
**1.2 s today**, not because sweeps were avoided.

The threshold is therefore corpus *size*, not analysis shape. At 34 MB/s, a 10× corpus (~420 MB,
~870 transcripts) puts a full parse near 12 s, which is where an interactive CLI starts to hurt.
Single-session work is flat regardless — individual sessions do not grow 10×.

So the trigger to revisit is specific and measurable: **sustained corpus-wide analysis at roughly
10× the current corpus, with a timing to show for it.** Not "we have more sessions now."

If that day comes, the cheap move is incremental parse-from-byte-offset, which the append-only
property makes trivial. Try that before reaching for a store again.

## 3. Division of responsibility

```
  ~/.claude/projects/**/*.jsonl   ← store of record (append-only)
            │
            ├──▶ tracker (TypeScript, container)
            │      watchers → parser.ts → Session → in-memory Map → HTTP/SSE → UI
            │                                                          ▲
            │      <SUMMARY_DIR>/<session-id>.md ──── read-only ───────┘
            │
            └──▶ sessionkit (Python, host)
                   parse on demand → classify → budgeted reports → stdout
                          │
                          └──▶ skills (Claude-driven, host)
                                 └─▶ write summaries to <SUMMARY_DIR>
```

**Two surfaces over one corpus, not a product and its prototype.** The tracker is in daily use and
stays the browsing surface: pick a project, pick a session, read it, edit config. sessionkit is
the command-line surface for the questions a three-panel UI cannot answer — statistics across
every session, failure clustering, agent-command review, forensics on a run that went wrong — and
for feeding those answers into a Claude session under a strict output budget. Neither replaces the
other.

The two paths read the same files independently and neither depends on the other. **sessionkit
works with the container down** — which matters, because the container is exactly what you cannot
rely on when you are debugging why something failed. That was an open question in the earlier
draft; it dissolves rather than being answered.

### 3.1 The write boundary

**This is easy to blur, so it is stated once, here.**

The `sk` CLI is **read-only**: it emits to stdout and writes nothing else. That invariant is what
lets it run against a live corpus with no risk of corrupting it. Since the cache was removed it
has no writable state at all.

The **skills** are Claude-driven procedures that consume `sk` output, and some of them write
files. A summary lands on disk because a skill wrote it, not because `sk` did.

Nothing writes a database. The only server-side state is the tracker's in-memory map, rebuilt from
disk at startup in about a second.

### 3.2 On two parsers

The earlier draft treated duplicate parsing as the problem to solve and proposed a shared store as
the fix. That trade is worse than the drift it prevents: it makes every analysis depend on the
tracker having recently run, introduces a version contract and a staleness banner, and — per §2 —
buys no speed.

The rejected alternatives were reusing `parser.ts` through the dev container (couples every skill
to `docker compose` being up) or through the HTTP API (same availability problem, and it serves
UI-shaped `Session` objects rather than error clusters).

Drift is a real cost and it is paid in one place: both parsers must learn a new transcript field
independently. Accept it, and keep the divergence note in `CLAUDE.md` current.

## 4. Data completeness audit

Analysis no longer depends on the tracker's parser, so these gaps are not blocking — but they are
real, and they still limit what each surface can show. This table is the authority for the
cross-parser comparison.

| Analysis need | Data required | `parser.ts` (tracker/UI) | `parse.py` (sessionkit) |
|---|---|---|---|
| Which tool calls failed | `tool_result.is_error` | ❌ Never read (`parser.ts:364-372`) | ✅ `parse.py:57` |
| What the failure said | tool result text | ⚠️ Truncated to 2000 chars (`parser.ts:368`) | ✅ Preview + true length |
| Tool loops | stable digest of tool input | ❌ No digest | ✅ `input_digest` |
| Session interrupted mid-tool-call | `tool_use` with no matching result | ⚠️ Pairing exists; unmatched not flagged | ✅ `no-result` class |
| Subagent killed | `system.subtype = agents_killed` | ❌ Only `away_summary` (`parser.ts:326`) | ✅ Typed system events |
| Context exhaustion | `compact_boundary` + `preTokens` | ❌ | ✅ |
| Stalls / latency outliers | `turn_duration.durationMs` | ⚠️ Tool durations only | ✅ |
| Cost attribution | per-message usage × correct rates | ✅ Fixed 2026-08-26 | ✅ |
| File thrash / read loops | `fileChanges` with operation | ✅ | ✅ |
| Hook behaviour | hook events | ✅; subagent `progress` records ❌ | ✅ |
| Permission decisions | permission events | ✅ | ✅ |
| File versions / rework | `file-history-snapshot` records | ✅ `parser.ts:96` | ❌ Drops all 340 |

**Each parser is incomplete, in opposite directions.** sessionkit's was written for error analysis
and captures the failure signal `parser.ts` never reads; `parser.ts` was written to feed a UI and
reads the file-version records sessionkit drops. That asymmetry is the honest reason both exist,
and it is a smaller problem than "two parsers."

But note that **neither gap announced itself.** Both parsers silently ignore record types they do
not know, which is why `PLAN.md` carries a round-trip validator as an open decision.

### 4.1 The only cross-repo facts that matter

Everything else about the tracker belongs in the tracker's own repo. These two can **silently**
break agreement between the surfaces, so a change to either must be recorded in both places:

- **Pricing rates.** `sessionkit/pricing.py` and the tracker's `server/src/pricing.ts` are two
  tables over one set of facts. Change one, change the other. `sk cost` and the UI's Costs tab
  must agree to the cent.
- **`tool_result.is_error`.** Until `parser.ts` reads it, the UI cannot show which calls failed
  while `sk errors` clusters them by cause. Any error-related claim about "the UI" depends on this.

## 5. Roadmap

**opencode — deferred, and it does not fit this decision.** opencode has no JSONL transcripts; its
sessions live in `~/.local/share/opencode/opencode.db`, already a SQLite database. "Read the
filesystem" has nothing to read there, and `opencode-parser.ts` must query a database regardless.
So *no database* is a property of the Claude Code path, not of the whole system.

Separately, opencode's schema exposes no tool-call error flag, so error analysis would be silently
thin for that source rather than absent — the worst failure mode for a report. Until it is handled
properly, reports state which sources they cover, and `kind: opencode` is excluded from error
clustering rather than quietly under-counted.

**Fleet-wide analysis.** Not on the roadmap — it is shipping. `sk errors` already clusters every
failed tool call across the corpus. No store was needed for any of it. Revisit persistence only at
the ~10× threshold in §2.2, with measurements.

**Cross-machine aggregation.** If corpora from several hosts ever need merging into one queryable
store, that is a genuinely different problem and this decision does not cover it.

## 6. Division of ownership

Three documents. Each answers one question, and **a fact is updated in exactly one of them.**
Others may reference it by section; none may restate it.

| Document | Answers | Owns |
|---|---|---|
| `SPEC.md` (this file) | *Why is it built this way?* | The no-database decision and the threshold to revisit it; corpus measurements; the cross-parser completeness audit; the write boundary |
| `PLAN.md` | *What are we building, and what's next?* | The CLI/skill dividing rule; command and skill scope; phases and acceptance criteria; open decisions |
| `NOTES.md` | *What did we learn, and where from?* | Prior-art survey; borrowed thresholds; changelog; superseded reasoning |

Rules that keep them honest:

- **Neither plan document duplicates the other.** This file states no phases; `PLAN.md` re-derives
  no rationale for the no-database decision.
- **History goes to `NOTES.md`.** If a sentence starts "we used to", it belongs there.
- **Cross-package changes get recorded in both repos** — they are the only ones that can silently
  invalidate a document elsewhere. The list is §4.1 plus `SUMMARY_DIR`.
