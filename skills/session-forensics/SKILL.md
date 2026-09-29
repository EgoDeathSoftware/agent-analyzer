---
name: session-forensics
description: Post-mortem for ONE coding-agent session (Claude Code, pi, or any other reachable source) — what went wrong (crash, stall or hang, loop, drift or hyperfocus, tool-calling failure), why (the decisive error), and what to change (instructions file, harness config, serving stack, model, environment). Use when asked why a specific session failed, looped, stalled, drifted or produced a bad result, for a "post-mortem" of a session, or what an agent (including a subagent) actually ran. Triggers on "why did this session fail", "post-mortem", "what went wrong with this agent", "why did it loop", "why did it drift", "what did that agent actually run", "review this session". NOT for what keeps failing across many sessions — use error-patterns for that.
---

# Session Forensics — the post-mortem

Diagnose one coding-agent session (or subagent) with `sk forensics`, then explain it: what went
wrong, the decisive error behind it, and fixes aimed at the right target. **Read-only**: name
findings and propose changes; never edit instructions files, hooks, settings or models.

## Boundaries with neighbouring skills

| Ask | Skill |
|---|---|
| "Why did *this* session fail / loop / stall / drift?" | **this skill** |
| "What keeps failing across my sessions / which model is worse?" | `error-patterns` |
| "What was I working on / what's unfinished?" | `unfinished-work` |
| "Audit this project's CLAUDE.md / hooks / MCP config" | `auditing-claude-projects` |

## Tool

All analysis goes through the sessionkit CLI. **Never read transcripts directly.** Resolve
`$SK` in order and stop with a named error if all three miss:

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

### 1. Select the session

Use the id or prefix the user gave. Otherwise, find candidates:

```bash
$SK index --finding degeneration          # sessions with a given finding
$SK index --state agent-unresponsive      # or aborted, error-cascade, interrupted-tool, ...
$SK index --source pi --since 7d          # pi sessions only
```

**Subagents** are separate sessions: `$SK children <parent-sid>` resolves each dispatch to
its child. That includes pi-subagents async runs; list them with `--subagents include`. When
the parent's failure involves a child (`orphan-subagent`, `resource-interference`), analyse
the child too.

### 2. Run the report

```bash
$SK forensics <sid> --json
```

- **Meta:** `harness`, `model`, `state` and the end reason. `agent-unresponsive` means the
  agent stopped answering; the user didn't walk away.
- **`findings`:** `tier`, `family`, `kind`, `detail`, `count`, `lines`, `fix_target`, `hint`.
- **`focus`:** per user turn, where the effort went. Turns holding a drift candidate are
  expanded into runs with `share` and `referenced`.
- **`timeline`:** only the lines the findings cite.
- **`health`:** tool success, stop reasons, empty turns, the reasoning/visible ratio,
  compactions and median turn latency.

### 3. Triage by tier

- **`S` findings** are facts. Still read their cited lines, because the report must explain
  *why*, not restate the kind.
- **`candidate` (S->J) findings** are hypotheses. Confirm or dismiss each one with one line of
  evidence:

  ```bash
  $SK show <sid> --mode messages --range 40:60
  ```

  Report dismissals too; they show the reader where the detector was wrong.

For each kind present, read its entry in `reference/failure-classes.md`. That entry says how
to confirm the finding and what the fixes are. Load only the entries you need.

### 4. Judgment checklist

Always run these checks. Keep it bounded: about ten `show` calls in total.

- **False premise:** walk back from the first failure to the first assumption the agent acted
  on without checking it. This is the most common decisive error.
- **Spec neglect:** list the ask's requirements (the ask is the `focus` row's `ask`) and mark
  each as done or not done.
- **Misread output:** find a tool result that contradicts the next action.
- **Ignored own warning:** find a hazard or constraint the agent itself stated ("I need to be
  careful…", "must not…"), then check whether later actions violated it.
- **Role drift:** in an orchestrated session, check whether the controller did its delegate's
  work while the delegate was running.
- **Unflagged drift:** for turns whose ask had no references and no `user-interrupt`, read the
  turn's runs in `focus` and judge them yourself.

### 5. Find the decisive error

The decisive error is the earliest step after which failure was inevitable. Cite it
separately from the line where the failure *surfaced*. In published trajectory studies the
median decisive error comes around step 7 and surfaces about 10 steps later, so do not stop at
the first red line.

### 6. Prescribe fixes by target

| Target | Claude Code | pi |
|---|---|---|
| Instructions | `CLAUDE.md`; hooks for enforcement | `AGENTS.md` (pi also reads `CLAUDE.md`), `.pi/SYSTEM.md`, a `tool_call` extension |
| Harness config | `settings.json` | `models.json` (`maxTokens`, `contextWindow`, `compat`), `settings.json` (thinking level) |
| Serving | — | vLLM `--tool-call-parser`, chat template, sampling (temperature, repetition penalty) |
| Model | swap | swap |
| Env | auth, network, rate limits, shared resources | same |

Only recommend a model swap with a rate behind it:

```bash
$SK findings --group-by model --kind <kind>
```

If the model's group has fewer than 10 sessions, say the rate is not meaningful yet.

### 7. Report

```
Verdict: <one line: what went wrong>
Decisive error: line N — <what>; surfaced at line M
Confirmed: <kind (lines) — why, in one line> …
Dismissed: <kind (lines) — why not> …
Judgment checks: false premise / spec neglect / misread output / own warning / role drift
Fixes (ranked by expected impact): 1. [target] <change> — <why it addresses the decisive error>
Health: <success rate, stop reasons, median latency>
```

## Rules

- **Read-only.** Propose changes; never apply them.
- **A clean session is a valid result.** Don't manufacture a finding.
- **Never claim coverage you don't have.** If a table ends with `… N more row(s) omitted`, say
  so or raise `--budget-kb`.
- **A live session's unfinished tail is expected.** `hung` and `abrupt-truncation` are
  suppressed for live sessions; don't re-derive them by hand.
- **Name small samples.** Example: "pi: 4 sessions — rates not meaningful".
