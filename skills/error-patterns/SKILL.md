---
name: error-patterns
description: Find recurring tool failures across ALL Claude Code sessions on this machine and propose concrete fixes (CLAUDE.md rules, hook changes, missing tools). Use when asked why sessions keep failing, what keeps breaking, which errors repeat, or for a periodic health check of the session fleet. Triggers on "what keeps failing", "recurring errors", "why do my sessions error", "error patterns", "session health check". NOT for reviewing a single session's failure — use session-forensics for that.
---

# Error Patterns

Cluster every failed tool call across every reachable Claude Code session and turn the top
clusters into fixes.

This is a **fleet-wide, read-only** analysis. It never edits CLAUDE.md, hooks, or settings — it
proposes changes and the user applies them.

## Boundaries with neighbouring skills

Route correctly before you start; these three overlap in the obvious reading:

| Ask | Skill |
|---|---|
| "What keeps failing across my sessions?" | **this skill** |
| "Why did *this* session fail / loop / stall?" | `session-forensics` |
| "Why is this project expensive?" | `cost-forensics` |
| "Audit this project's CLAUDE.md / hooks / MCP config" | `auditing-claude-projects` |
| "Stop asking me for permission so often" | `fewer-permission-prompts` |

If the user named one session, you are in the wrong skill.

## Tool

All analysis goes through the sessionkit CLI. **Never read transcripts directly** — the corpus
is tens of megabytes and the CLI exists to keep it out of your context.

```bash
SK=/mnt/c/Users/david/Projects/CAT_AI/claude-session-analyzer/sk
```

If that path does not exist, say so and stop; do not fall back to grepping `~/.claude`.

## Procedure

### 1. Establish scope

```bash
$SK doctor
```

Read the **Sources** table. If any source is unreachable, that is a finding in itself — say
which ones and state plainly that the report covers only what was visible. A fleet report that
silently covers one of four machines is worse than no report.

### 2. Cluster by class

```bash
$SK errors --subagents include            # add --since 30d / --project <name> to narrow
```

The "Reading this" footer names three things: the largest class, the class with the widest
reach, and the dominant single signature. **Rank by breadth, not count.** A class with 60
failures in 3 sessions is usually one broken script; a class with 40 failures across 30
sessions is a systemic problem worth fixing.

### 3. Drill into the top clusters

```bash
$SK errors --group-by signature --budget-kb 6   # exact repeated error strings
$SK errors --group-by tool                      # which tool is failing
$SK errors --group-by session                   # is it one bad session or many?
```

### 4. Confirm a cause on a real session

Only now open individual sessions, and only the two or three that matter:

```bash
$SK index --state interrupted-tool
$SK show <sid> --mode errors
$SK show <sid> --mode timeline --budget-kb 6
```

### 5. Report

For each of the top 3–5 clusters, give:

- **What** — the class and signature, with counts and *sessions affected out of total*
- **Why** — the root cause, confirmed against a real session, not inferred from the class name
- **Fix** — a concrete, applyable change

Then ask whether to apply any of them. Do not apply unprompted.

## Fix types, in order of preference

1. **A CLAUDE.md rule** — best for anything self-inflicted and predictable. Stops the bad call
   from being emitted at all.
2. **A hook change** — only when the behaviour must be enforced rather than requested.
3. **Install/remove a dependency** — for `missing-tool`.
4. **A permission allowlist entry** — hand off to `fewer-permission-prompts` rather than
   duplicating it here.
5. **Nothing** — `exit-code` is often genuine command failure and needs no systemic fix. Say so
   rather than inventing a recommendation.

## The hook-block trap

The most common finding on this machine is a `PreToolUse` hook rejecting a command *after* the
model emitted it — currently **73 failures across 43 of 86 sessions**, the largest class by
both count and reach. Every one of those is a wasted round trip.

When you see this, the fix is almost never a better hook. The hook is working; the model never
learned the rule, because a hook fires at execution time and teaches nothing at generation
time. The fix is a line in CLAUDE.md stating the rule directly, e.g.:

> Use `rg` instead of `grep` and `fd` instead of `find` in Bash commands.

Keep the hook as enforcement, and add the rule as instruction. Check whether the rule is
already present but buried or ambiguously worded before recommending a new one.

## Interpreting classes

| Class | Usually means | Usually fixed by |
|---|---|---|
| `hook-block` | The model doesn't know a local rule | A CLAUDE.md line |
| `user-rejected` | Approach problem, not a bug | Clearer instructions about what not to attempt |
| `missing-tool` | Environment drift | Install it, or drop the dependency |
| `file-too-large` | Unbounded reads | Instruct offset/limit or Grep |
| `permission-denied` | Writing outside a writable path | Name the writable path in CLAUDE.md |
| `not-found` / `not-a-repo` | cwd assumptions | State the layout in CLAUDE.md |
| `stale-read` | Concurrent writers | Re-read before edit |
| `rate-limit` / `api-error` | External | Backoff, or a different provider |
| `exit-code` | Genuine failure | Often nothing systemic |
| `no-result` | Session interrupted mid-call | Hand off to `unfinished-work` |
| `other` | Taxonomy gap | Report the signature so the taxonomy can grow |

A large `other` bucket is a finding: it means the taxonomy is missing a real class. Surface the
top `other` signatures rather than ignoring them.

## Rules

- **Read-only.** Propose fixes; never edit configuration yourself.
- **Never claim coverage you don't have.** If `doctor` shows unreachable sources, or a report
  ends with `… N more row(s) omitted`, say so explicitly.
- **Confirm before recommending.** A class name is a hypothesis; open one real session and
  check the actual command before you propose a fix for it.
- **Don't pad the list.** Three real findings beat eight speculative ones.
