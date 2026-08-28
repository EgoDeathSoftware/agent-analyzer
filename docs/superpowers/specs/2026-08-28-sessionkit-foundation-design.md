# Foundation pass: what Phases 3–8 all need first

**Date:** 2026-08-28 · **Status:** Designed, not built.

`PLAN.md` describes Phases 3–8 with acceptance criteria. Four pieces of work sit underneath
them: each is deferred in `PLAN.md` with a note like *"its own pass"* or *"prerequisite"*, each
is needed by more than one phase, and none of them belongs to any single phase. This document
specs those four and nothing else.

Built per phase instead, each would be built four to six times, slightly differently — the
"distributed worse" failure `PLAN.md` §2 rejects for the parser, in a different costume.

**Scope:** the four items in §2. Explicitly **not** in scope: every Phase 3–8 command and
skill, and all nine open decisions in `PLAN.md` §10.

---

## 1. What probing changed

Four facts were verified against this container's corpus before designing. Three contradict
`PLAN.md`, so they are recorded here and corrected there.

- **`CLAUDE_CODE_CHILD_SESSION` does not identify a subagent.** `PLAN.md` §5.3 says a skill can
  tell it is running inside a subagent by this variable. It is set to `1` in a top-level
  interactive session — verified: session `560ec2d2…`, matching `sessions/234.json` with
  `"kind":"interactive"`. Nothing may consult it. The guard `PLAN.md` §5.3 wants is the other
  sentence it already contains: **skills pass the handle explicitly and never inherit it.**
- **`file-history-delta` exists** (19 records here) alongside `file-history-snapshot` (26).
  `PLAN.md` §3.2 names only the latter. A parser taught one type and not the other is silently
  incomplete in exactly the way §4 of `SPEC.md` warns about.
- **Record counts are per-corpus, not a parser contract.** `PLAN.md` quotes 340
  `file-history-snapshot` records; this container sees 26. Both are true of their own corpus.
  Acceptance criteria here are written against record *types*, never counts.
- **A `custom-title` transcript record type exists** (67 occurrences), beyond the four name
  sources `PLAN.md` §5.3 lists. Noted, not used — see §2.1.

## 2. The four items

### 2.1 Handle resolution — `handles.py`

**Problem.** `sk show`, `sk forensics` and `sk commands` each take a *required* session id and
prefix-match it (`corpus.py:123`). `PLAN.md` §7 records that Phase 2 deferred the rest
deliberately. Phases 3, 5, 6 and 7 each add session-scoped commands, so each would otherwise
inherit a required-argument-only handle or grow its own resolution.

**Design.** One module, one public function: `resolve(handle: str | None) -> Loaded`.

Resolution order, chosen by whether an argument was given:

| Argument | Order |
|---|---|
| omitted | `$CLAUDE_CODE_SESSION_ID` → error (exit 3) |
| given | id or unique prefix (existing `corpus.load_session`) → substring match against `<session>/custom-title.json` |

A step matching **several** sessions stops there and does not fall through: print the candidates
(id, name, ended, project) and exit **4**, selecting nothing. No match anywhere: exit **3**.
Silently showing the wrong session is the failure mode `load_session` already guards against for
ids, and it must not reappear for names.

Session arguments on `show`, `forensics` and `commands` become optional. `cli.py` resolves once
before dispatch; commands receive a resolved entry and never re-derive one.

Every report's first line names the resolved target: `target: 560ec2d2 claude-folder-docs
(live)`. A default that is not shown is a default that gets misread.

**Deliberately not built:** `@last`, name lookup from `sessions/*.json` or from the
`custom-title` / `ai-title` / `agent-name` transcript records, and a JSON envelope for the
ambiguous case. `custom-title.json` is a sidecar costing a few hundred bytes per session; the
transcript records cost a parse of the whole corpus to search. If sidecar-only lookup proves too
thin in use, the transcript records are the next step — with a measurement, not a guess.

### 2.2 Live sessions — `sources.live_sessions()`

**Problem.** A running session always has an unmatched `tool_use` at the tail of its transcript,
which `derive_end_state` classifies as `interrupted-tool`. Two consequences, both wrong, both
otherwise arriving as bugs: Phase 3's `sk unfinished` reports every concurrently running session
as abandoned work, and §2.1's no-argument default — now the normal invocation — reports the
session you are sitting in as interrupted every single time.

**Design.** `sources.live_sessions()` reads `~/.claude/sessions/*.json` per source and returns
the set of running session ids. Files there are removed on exit, so absence means "not running".

`derive_end_state` gains a `live: bool` parameter and, when it is set, ignores the trailing
unmatched `tool_use`. The flag is passed in rather than read inside `classify`, which keeps the
taxonomy a pure function of the transcript and testable without a registry on disk.

Consumers: §2.1 marks the target `(live)`; Phase 3 filters on it.

### 2.3 Parser additions — `parse.py`

Two additions, each a stated prerequisite of a later phase.

**`file_versions`** — rows from `file-history-snapshot` **and** `file-history-delta`, carrying
path, version, `backupTime`, `messageId`, and which record type produced the row. `PLAN.md`
§3.2 calls this the prerequisite for Phase 6 and specifies the index: the transcript's own
records, anchored to a `messageId`, so a rewrite ties to the turn that caused it — with
`file-history/`'s `sha256(abs_path)[:16]` scheme as a content fallback, not the index.

**`content_bytes` and `result_bytes`** on existing tool rows, plus `spilled` and the spill path.
`PLAN.md` §3.2.1 measures these two at 2,165 B against 98,075 B on one call — 45×. Both are stored because the divergence is itself the signal ("produced 94 KB, paid for
2 KB" is what tells you spilling worked). Phase 5's `--bloat` ranks on `content_bytes` alone:
ranking on `result_bytes` would put every spilled call at the top of the report and invert the
finding, since spilling is the mitigation.

### 2.4 Two one-liners

- **`error-patterns` resolves `sk` instead of hardcoding it.** It opens with
  `SK=/mnt/c/Users/david/Projects/…` — a host path that does not resolve in the container this
  repo is also worked on from, so the skill's own guard fires and it stops, correctly but
  uselessly. Replace with `$SK` → `command -v sk` → the repo path. Every later skill copies the
  same three lines.
- **`sk doctor` prints effective `cleanupPeriodDays` per source**, marked *set* or
  *default (30)*. This is a Phase 2 acceptance criterion that `PLAN.md` does not mark met.
  Until it prints, "no errors before March" and "March was swept" are the same output.

## 3. Testing

stdlib `unittest`, inline fixtures, per `PLAN.md` §8. The suite is **138 tests** today, not the
96 `PLAN.md` states; that figure is corrected as part of this work.

Three negatives are required, because each covers a way this work can silently lie rather than
fail:

- A transcript with a dangling `tool_use` **and** a live registry entry does not appear as
  interrupted, and does not appear in unfinished-work reports.
- An ambiguous name exits 4 with candidates listed, rather than resolving to the newest.
- No code path reads `CLAUDE_CODE_CHILD_SESSION` (§1).

Plus: a sidecar-hit name lookup parses zero transcripts, asserted by counting parses rather than
by timing.

## 4. Acceptance

- `sk forensics` with no argument resolves to `$CLAUDE_CODE_SESSION_ID`, prints the resolved
  target on line 1 marked `(live)` where it applies, and does not report `interrupted-tool`
  purely because the session is still running.
- `sk show <name>` resolves a `custom-title.json` name; an ambiguous name exits 4 listing
  candidates; an unknown one exits 3.
- `parse.py` emits `file_versions` rows for both `file-history-snapshot` and
  `file-history-delta`, and tool rows carry `content_bytes` and `result_bytes` with the spilled
  flag set where the two diverge.
- `sk doctor` names the effective retention per source alongside reachability.
- `error-patterns` runs in the container without editing.
- The full suite passes, including the three negatives in §3.

## 5. Documents to correct

Per `SPEC.md` §6, a fact lives in one document. This work invalidates three statements in
`PLAN.md`, which are fixed there rather than restated here: the `CLAUDE_CODE_CHILD_SESSION`
claim in §5.3, the 96-test figure in §8, and §3.2's omission of `file-history-delta`.
