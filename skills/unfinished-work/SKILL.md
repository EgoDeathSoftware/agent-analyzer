---
name: unfinished-work
description: Find sessions that look abandoned mid-task and report what is at risk of being lost — in-flight tool calls, uncommitted files, a stated next step. Use when asked "what did I have in flight", "what was I working on", "resume", "abandoned sessions", "what's unfinished", or "what's at risk of being lost". NOT for why one specific session failed — use session-forensics for that — and NOT for fleet-wide error patterns — use error-patterns for that.
---

# Unfinished Work

Find every session that ended without finishing, rank it by how recoverable it is, and hand back
a cold-start brief for each one worth resuming — session title, last completed step, in-flight
tool call, uncommitted files, and a paste-ready `claude --resume <sid>` line.

This is a **fleet-wide, read-only** analysis. It never resumes a session, runs `claude`, or edits
anything — it reports, and the user decides what to resume.

## Boundaries with neighbouring skills

| Ask | Skill |
|---|---|
| "What's unfinished / what was I working on / resume?" | **this skill** |
| "Why did *this* session fail / loop / stall?" | `session-forensics` |
| "What keeps failing across my sessions?" | `error-patterns` |
| "Why is this project expensive?" | `cost-forensics` |
| "Which files got rewritten the most / churn?" | `edit-churn` |

If the user named one specific session and asked why it went wrong, you are in the wrong skill.

## Tool

All analysis goes through the sessionkit CLI. **Never read transcripts directly** — that is the
whole reason the CLI exists (`PLAN.md` §2).

Resolve `$SK` in order, and stop with a named error if all three miss — do not fall back to
grepping `~/.claude`:

```bash
if [ -n "$SK" ] && [ -x "$SK" ]; then
  :  # already set
elif command -v sk >/dev/null 2>&1; then
  SK=sk
elif [ -x /workspace/sk ]; then
  SK=/workspace/sk
else
  echo "sk not found: set \$SK, put sk on PATH, or run from the sessionkit repo" >&2
  exit 1
fi
```

## Procedure

### 1. Find candidates

```bash
$SK tail --all --json --n 4 --budget-kb 12
```

This already excludes `complete` sessions and anything currently running (read from
`sessions/*.json`) — a live session always carries a trailing unmatched tool call and would
otherwise look abandoned. Each candidate row carries `sid`, `state` (the session's `end_state`),
`tail_signal`, `ended`, and a `tail_excerpt`.

Narrow with `--since`/`--project` if the user named a window or a project.

### 2. Annotate with what's at risk

For each candidate:

```bash
$SK files <sid> --uncommitted --json
```

This intersects the session's touched files against `git status --porcelain` in the session's own
cwd. A candidate with zero uncommitted files has nothing left exposed on disk even if the
transcript looks unfinished — that changes which bucket it lands in.

### 3. Bucket every candidate

Bucket on `tail_signal` (from step 1) crossed with the uncommitted count (from step 2) and age
(`ended` vs now):

| `tail_signal` | Dirty files | Age | Bucket |
|---|---|---|---|
| `mid_tool` | >0 | <24h | **Resume now** |
| `apology` / `silent` / `error_tail` | >0 | any | **Review before resuming** |
| `completion_marker` | 0 | any | **Probably done** |
| any, `state` is `killed-agents` or `interrupted-user` | 0 | any | **Sunk** (close) |

A candidate that matches none of these rows (e.g. `next_step_stated` with no dirty files, or
`mid_tool` but stale) still gets reported — put it in **Review before resuming** rather than
dropping it; the table is a starting heuristic, not an exhaustive partition.

### 4. Handoff brief for every Resume/Review candidate

For each session in **Resume now** or **Review before resuming**, pull:

```bash
$SK index --project <name>        # session title / label, first prompt
$SK tail <sid> --n 6              # last completed step, in-flight tool call if mid_tool
$SK files <sid> --uncommitted     # the uncommitted paths, already fetched in step 2
```

Report, per session:

- **Title / label** (from `sk index`) and the id, so the report reads as a name a human
  recognizes, not a bare UUID.
- **Last completed step** — the last assistant turn or tool result before the tail, in one line.
- **In-flight tool call**, if `tail_signal` is `mid_tool` — the specific call left without a
  result.
- **Uncommitted files** — the paths from step 2, not a count.
- **Stated next step**, if `tail_signal` is `next_step_stated` fired anywhere in the fetched tail
  — quote it; it is often the exact instruction to hand back to the resumed session.
- **A paste-ready line**: `claude --resume <sid>`. Print it; never run it — resuming a session is
  the user's call, not this skill's.

### 5. Subagent sessions

If `sk tail <sid>` or `sk index` shows the candidate is a subagent (it will report a `parent_sid`
if it has one), say so explicitly: resuming means resuming the **parent** session, not the
subagent transcript directly, since a subagent has no independent `claude --resume` handle.

### 6. Report totals

Close with one line per bucket: how many candidates landed in each of Resume now / Review before
resuming / Probably done / Sunk, out of the total candidates found. Put **Probably done** and
**Sunk** at the bottom as short lists (id + title only) — they are the ones the user does not need
a handoff brief for.

## Rules

- **Read-only.** Report and hand back `claude --resume` lines; never invoke `claude`, never edit
  or delete anything.
- **Never claim coverage you don't have.** If `sk tail --all` reports omitted rows, say so, or
  raise `--budget-kb`.
- **Rank by recoverability, not recency.** A session from three days ago with a stated next step
  and dirty files is more actionable than one from an hour ago that already looks done.
- **A candidate with no findings in any bucket is still reported**, in Review before resuming —
  don't silently drop ambiguous cases to keep the table tidy.
