---
name: session-forensics
description: Diagnose why ONE Claude Code session went wrong — looped, stalled, killed a subagent, or ended interrupted — and what actually ran during it. Use when asked why a specific session failed, looped, stalled, or produced a bad result, or to review what commands/tools an agent (including a subagent) actually invoked. Triggers on "why did this session fail", "why did it loop", "what did that agent actually run", "review this session". NOT for a fleet-wide question about what keeps failing across many sessions — use error-patterns for that.
---

# Session Forensics

Diagnose a single Claude Code session (or subagent) using `sk forensics` and `sk commands`. This
is a **single-session, read-only** analysis — it names what went wrong, cites the exact lines,
and ends with a prevention hint, never a rewritten narrative of the transcript.

## Boundaries with neighbouring skills

| Ask | Skill |
|---|---|
| "Why did *this* session fail / loop / stall?" | **this skill** |
| "What keeps failing across my sessions?" | `error-patterns` |
| "Why is this project expensive?" | `cost-forensics` |
| "Audit this project's CLAUDE.md / hooks / MCP config" | `auditing-claude-projects` |

If the user did not name a specific session (or "the current session" / "the one I'm in"), you
are probably in the wrong skill — check whether they mean the whole fleet.

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

### 1. Identify the session

The user will usually name a session id, a prefix, or "this session" / "the current one". A
session id is required today (`sk forensics <sid>`) — if you don't have one, list candidates:

```bash
$SK index --state interrupted-tool     # sessions that look unfinished
$SK index --project <name>             # narrow by project if the user named one
```

### 1a. A killed or orphaned subagent — find it from its parent

If a finding names `agent-kill` or `orphan-subagent`, the child transcript is a **separate**
session, filtered by **its own** project — which is the worktree/cwd it ran in, not the parent's
project name. Resolve it directly rather than guessing the project:

```bash
$SK children <parent-sid>     # every Agent dispatch from this session, resolved to its child sid
```

A dispatch reported as `resolved: no` has no `<task-notification>` match in the parent transcript
— treat that as "could not be resolved," not as "no child exists."

### 2. Findings and timeline

```bash
$SK forensics <sid>
```

Read top to bottom:

- **Findings** — every detected anomaly, each with a line-anchored prevention hint. A session
  with no findings is a real, positive result — say so plainly rather than padding the report.
  Two subagent findings look similar but mean different things: `agent-kill` is user-initiated
  (someone stopped it); `orphan-subagent` means it was dispatched and never returned at all,
  which usually means it stalled or the parent moved on without waiting. Name which one fired —
  "you pulled the plug" and "it vanished" call for different follow-ups.
- **Timeline** — only the lines the findings cite, not the whole transcript.
- **Health** — total tool calls, success rate. This is the "what went right" counterweight; a
  98% success rate on a session that looks alarming from its `end_state` alone is worth saying.

### 3. What actually ran

For "what did it (or its subagents) actually do":

```bash
$SK commands <sid-prefix-filter-not-supported> --group-by command   # fleet-wide, so narrow first:
$SK commands --group-by agent --agent-type Explore                  # per-agent, by type
$SK show <sid> --mode tools                                         # every call in this one session, in order
```

`sk commands` is fleet-wide by design (it has no session argument); for one session's own call
list, `sk show <sid> --mode tools` is the right layer-3 tool. Use `sk commands --group-by
session` only when comparing what several sessions ran.

### 4. Confirm before reporting

A finding's `kind` is a hypothesis about the pattern, not proof of the cause. Read the cited
lines in **Timeline** and, if the picture is still unclear, pull the surrounding turns:

```bash
$SK show <sid> --mode messages --range 40:60
```

### 5. Report

For each real finding: **what** (kind, count, lines), **why** (confirmed against the timeline,
not inferred from the kind name alone), and the **prevention hint** `sk forensics` already gave
you — restate it, don't invent a new one. Close with the Health numbers so the report isn't read
as pure negativity.

## Rules

- **Read-only.** Name findings and hints; never edit CLAUDE.md, hooks, or settings.
- **Never claim coverage you don't have.** If the report ends with `… N more row(s) omitted`,
  say so, or raise `--budget-kb`.
- **A clean session is a valid answer.** Don't manufacture a finding to have something to report.
- **The default target is often a live session.** If `sk forensics` reports `end_state:
  interrupted-tool` and the session in question is the one you're running in, that trailing
  unmatched tool call is expected, not a bug — note it rather than treating it as a finding.
