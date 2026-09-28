# Checkpoint 6: Skills and validation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this checkpoint task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Part of:** the agent post-mortem plan. Tracker: `2026-09-27-agent-postmortem-00-tracker.md`. Read its **Global Constraints**, **Spec deviations** and **Review Focus** before starting; they apply to every task here.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**Goal:** The failure-class knowledge base, the post-mortem and fleet skills, the README, corpus acceptance, the dry-run regression and real pi sessions.

**Starts from:** Checkpoint 5 is complete: its tasks are ticked in the tracker and the full suite passes.

**Done when:** every task below is committed; `PYTHONPATH=. python3 -m unittest discover -s tests -t .` passes; and the tracker marks each task done with its commit.

---

### Task 16: The failure-class knowledge base and parity tests

**Files:**
- Create: `skills/session-forensics/reference/failure-classes.md`
- Test: `tests/test_parity.py` (new)

**Interfaces:**
- Consumes: `taxonomy.ALL_CLASSES` (Tasks 1, 7–13) and `harness.ROLES` (Task 2).
- Produces: one `### \`<name>\`` entry for every taxonomy class. The skill (Task 17) loads the
  entries for the classes a report actually contains.

- [ ] **Step 1: Write the failing test**

Create `tests/test_parity.py`:

```python
"""Code and knowledge base stay in step: every taxonomy class is documented for the skill,
and every harness role is used by some detector."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from sessionkit import detectors, harness, taxonomy

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "skills" / "session-forensics" / "reference" / "failure-classes.md"


class ParityTest(unittest.TestCase):
    def test_every_class_has_exactly_one_reference_entry(self) -> None:
        headings = re.findall(r"^### `([a-z-]+)`", REFERENCE.read_text(encoding="utf-8"),
                              re.M)
        self.assertEqual(len(headings), len(set(headings)), "duplicate reference entry")
        names = {c.name for c in taxonomy.ALL_CLASSES}
        self.assertEqual(names - set(headings), set(), "classes missing from the reference")
        self.assertEqual(set(headings) - names, set(), "reference entries with no class")

    def test_every_role_is_used_by_a_detector(self) -> None:
        source = "".join(p.read_text(encoding="utf-8")
                         for p in Path(detectors.__file__).parent.glob("*.py"))
        for role in harness.ROLES:
            if role == "other":
                continue
            with self.subTest(role):
                self.assertIn(f'"{role}"', source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parity -v`
Expected: FAIL with `FileNotFoundError` for `failure-classes.md`. The roles test already
passes.

- [ ] **Step 3: Create `skills/session-forensics/reference/failure-classes.md`**

````markdown
# Failure classes — post-mortem reference

One entry per class in `sessionkit/taxonomy.py`; `tests/test_parity.py` keeps the two in step.
Load only the entries for classes the report actually contains.

Each entry gives:
- the tier: **S** = sk's verdict, **S->J** = a candidate you confirm, **J** = your judgment;
- the family;
- the fix target: **model**, **harness** (instructions file, harness config, token limits),
  **serving** (vLLM parser, chat template, sampling), **env**, or **none**.

Where instructions go:
- **Claude Code:** `CLAUDE.md` for rules, `settings.json` hooks for enforcement.
- **pi:** `AGENTS.md` for rules (pi also reads `CLAUDE.md`), `.pi/SYSTEM.md` for the system
  prompt, and a `tool_call` extension for enforcement.
- **pi model limits:** set per model in `~/.pi/agent/models.json` (`maxTokens`,
  `contextWindow`, `compat`). The thinking level lives in `settings.json`.

## Tool protocol

### `args-dropped`
S · tool-protocol · fix: serving
- **What:** the harness received a tool call with empty arguments (`Received arguments: {}`).
  pi repairs unparseable tool-call JSON to `{}`, so the model's arguments were lost between the
  model and the harness.
- **Confirm:** the model's own text or reasoning shows it meant to pass arguments. Several
  such failures in one session point to a parser problem, not a model mistake.
- **Fix:** check the vLLM `--tool-call-parser` and chat template for this model family.
  Upgrade vLLM if a parser fix exists; for pi, confirm the provider `compat` settings in
  `models.json`.

### `invalid-args`
S · tool-protocol · fix: model
- **What:** the arguments failed the tool's schema (wrong type, missing required field).
- **Confirm:** read the validation message. One slip is noise; repeats on the same tool mean
  the model misunderstands its schema.
- **Fix:** add a one-line usage example for that tool to the instructions file. If it
  persists on one model, compare rates with `sk findings --group-by model --kind
  invalid-args`.

### `unknown-tool`
S · tool-protocol · fix: model
- **What:** the model called a tool that does not exist (`Tool X not found`,
  `Unknown skill:`).
- **Confirm:** compare the name with the real tool list. Near-misses such as `bash_exec` for
  `bash` are hallucinations; names from another harness point to training carry-over.
- **Fix:** name the available tools in the instructions file. Remove stale tool names from
  skills and prompts.

### `truncated-call`
S · tool-protocol · fix: harness
- **What:** a tool call was cut off by the output token limit and never ran.
- **Confirm:** the assistant message's stop reason is `length`, and the call's arguments are
  large (a whole file in a write).
- **Fix:** raise `maxTokens` for the model (pi `models.json`), or tell the agent to write
  large files in parts.

### `edit-mismatch`
S · tool-protocol · fix: model
- **What:** an edit's old text was not found, was not unique, changed nothing, or overlapped
  another edit.
- **Confirm:** read the preceding read of that file. A stale view (an edit in between, or a
  formatter hook) or whitespace drift is the usual cause.
- **Fix:** add the rule "re-read the exact region before editing" to the instructions file.
  If a formatter hook rewrites files, say so there.

### `tool-misuse`
S · tool-protocol · fix: model
- **What:** a tool was pointed at the wrong kind of target, such as reading a directory
  (`EISDIR`) or using an offset past the end of the file.
- **Confirm:** check the path in the call against what an earlier listing showed.
- **Fix:** tell the agent to list or check a path before reading it. Usually a one-off.

### `sibling-cascade`
S · tool-protocol · fix: none
- **What:** a parallel call failed only because a sibling in the same batch failed.
- **Confirm:** find the sibling's own error; that is the real finding.
- **Fix:** none on its own; fix the sibling's failure.

### `file-too-large`
S · tool-protocol · fix: model
- **What:** a full read exceeded the tool's token cap.
- **Confirm:** the result names the file size.
- **Fix:** use offset/limit reads or search first. State this in the instructions file for
  repos with large files.

### `read-truncated`
S · tool-protocol · fix: model
- **What:** the read returned only part of the file.
- **Confirm:** check whether the agent later acted on content past the cut.
- **Fix:** use narrower reads; watch for false-premise edits based on unseen content.

### `stale-read`
S · tool-protocol · fix: model
- **What:** the file changed after the agent read it (another writer, hook or subagent).
- **Confirm:** look for a concurrent writer: a formatter, a parallel agent, or the user.
- **Fix:** re-read before editing. If agents run in parallel on one tree, give each its own
  worktree.

### `unread-file`
S · tool-protocol · fix: model
- **What:** a write or edit was attempted on a file that was never read.
- **Confirm:** there was no earlier read of the path.
- **Fix:** add the rule "read before editing". It is harmless but costs a round trip.

### `tool-text-leak`
S · tool-protocol · fix: serving
- **What:** the assistant wrote a tool call as plain text (`<tool_call>`, `<function=`, a bare
  `{"name": …, "arguments": …}` or harmony markup). No call ran, and the turn ended as if it
  were complete.
- **Confirm:** the cited message's text holds call markup and the next thing is the user
  re-prompting or the session ending. It is common on Qwen and gpt-oss, and worse at long
  context.
- **Fix:** check the vLLM `--tool-call-parser` and chat template for this model, and use a
  version with the parser fix. Keep the context smaller. If one model leaks repeatedly,
  compare it on `sk findings --group-by model --kind tool-text-leak`.

## Generation

### `empty-turn`
S · generation · fix: serving
- **What:** a turn ended normally with no text and no tool call, usually reasoning-only
  output.
- **Confirm:** the Health section shows reasoning on that turn and no visible text.
- **Fix:** check the reasoning parser and chat template, or lower the thinking level (pi
  `settings.json`).

### `degeneration`
S · generation · fix: serving
- **What:** one turn repeats itself: the same plan restated many times, typically in the
  reasoning.
- **Confirm:** read the cited message. Look for the point where it starts repeating, and what
  it was stuck on (often an unresolved ambiguity or an unreachable resource).
- **Fix:** adjust sampling for this model. Raise the temperature to the vendor's recommended
  value and add a repetition or presence penalty. Lower the thinking level. Keep the context
  smaller before hard steps. A 4-bit quantization degenerates more than a higher-precision
  one.

### `output-truncated`
S · generation · fix: harness
- **What:** a turn hit the output token limit.
- **Confirm:** the cited turn's stop reason is `length`.
- **Fix:** raise `maxTokens`, and keep the context-window reservation in mind (see
  `context-overflow`). Ask for smaller steps.

### `generation-stall`
S · generation · fix: serving
- **What:** one model turn took far longer than the session's norm.
- **Confirm:** check the turn for repetition (see `degeneration`), and check whether the
  server was busy then (see `resource-interference`). Median latency in Health tells a slow
  model from a stalled turn.
- **Fix:** same as `degeneration` when repetition is present. Otherwise address the server's
  load, or set a request timeout in the harness.

## Loops

### `repeat-tool`
S · loop · fix: model
- **What:** the identical call (same tool, same input) ran three or more times. Polling tools
  are exempt.
- **Confirm:** check whether the results changed between calls. If nothing changed, the agent
  ignored the result.
- **Fix:** add the rule "when a command's result is unchanged, stop and diagnose" to the
  instructions file.

### `file-thrash`
S · loop · fix: model
- **What:** one file was edited many times, or several times with failures in between.
- **Confirm:** read the edit sequence. Planned incremental edits are fine; undo/redo cycles
  are not.
- **Fix:** plan the full change before editing. Use one multi-edit call.

### `read-loop`
S · loop · fix: model
- **What:** the same file was re-read five or more times. Reads after an edit or a compaction
  are not counted.
- **Confirm:** check whether the agent used what it read.
- **Fix:** tell the agent to keep notes of what it read. Frequent loops can mean the context
  is too small for the task.

### `error-cascade`
S · loop · fix: model
- **What:** three or more consecutive failing calls.
- **Confirm:** check whether the calls were variants of one approach. The first failure's
  cause is usually the finding.
- **Fix:** add the rule "after two failures, stop and diagnose the root cause" to the
  instructions file.

### `hook-pingpong`
S · loop · fix: harness
- **What:** the same tool was blocked by a hook repeatedly.
- **Confirm:** the model kept emitting the forbidden form.
- **Fix:** state the rule in the instructions file (`CLAUDE.md`, or `AGENTS.md` for pi) and
  keep the hook as enforcement.

### `rejection-persist`
S · loop · fix: model
- **What:** a call the user rejected was retried unprompted.
- **Confirm:** the retry has the same input as the rejected call.
- **Fix:** add the rule "a rejection means ask, don't retry" to the instructions file.

### `edit-mismatch-spiral`
S · loop · fix: model
- **What:** edits to one file failed three or more times.
- **Confirm:** check whether the agent re-read the file between attempts.
- **Fix:** add the rule "re-read the exact region before each edit". On weak models, prefer
  whole-file writes for small files.

### `read-only-spiral`
S · loop · fix: model
- **What:** a long run of reads and searches with no edit, command or answer.
- **Confirm:** check whether the reads converged on something. Aimless exploration and
  necessary discovery look alike, so read the run's paths.
- **Fix:** ask for a plan or a first change. Add the rule "state a hypothesis after N
  reads". For open models, a smaller first task.

## Lifecycle

### `aborted`
S · lifecycle · fix: none
- **What:** the user stopped a call or turn.
- **Confirm:** see `user-interrupt` for what came next.
- **Fix:** none on its own.

### `killed`
S · generic · fix: env
- **What:** a command was killed (SIGKILL, usually out-of-memory) or terminated.
- **Confirm:** check the command and the machine's memory at the time.
- **Fix:** reduce the command's footprint or raise the limit.

### `agent-kill`
S · lifecycle · fix: none
- **What:** a subagent was killed before returning.
- **Confirm:** check whether the user stopped it or a timeout did.
- **Fix:** if it was a timeout, raise it or narrow the delegated task.

### `orphan-subagent`
S · lifecycle · fix: harness
- **What:** a subagent was dispatched and never returned.
- **Confirm:** run `sk children <sid>`. An unresolved dispatch means no result ever came
  back.
- **Fix:** check the subagent's own session for a hang; make the parent wait or poll.

### `compaction-churn`
S · lifecycle · fix: harness
- **What:** the context was compacted more than once.
- **Confirm:** check whether behavior changed after a compaction, such as forgotten
  constraints.
- **Fix:** split the work, narrow the scope, or use a model with a larger window.

### `stall`
S · lifecycle · fix: env
- **What:** a tool call ran far too long. Commands with a declared timeout only count near
  that timeout.
- **Confirm:** check what the command was waiting on: network, a lock, or an interactive
  prompt.
- **Fix:** give the command a timeout. Avoid interactive commands.

### `hung`
S · lifecycle · fix: env
- **What:** the session ended waiting on a call, or on a model turn that never finished.
- **Confirm:** check what was pending and whether its server or process was alive.
- **Fix:** fix the pending tool or server; set timeouts.

### `abrupt-truncation`
S · lifecycle · fix: none
- **What:** the transcript ends in a partial write, meaning the agent process died.
- **Confirm:** check for an OOM, a crash log, or the terminal being closed.
- **Fix:** investigate the harness process (memory, crashes); there is nothing in the
  transcript to fix.

### `user-interrupt`
S · lifecycle · fix: none
- **What:** the user stopped the agent and redirected it.
- **Confirm:** the redirect message says what the user wanted instead. Compare it with what
  the agent was doing (the paired `drift-candidate`).
- **Fix:** comes from the confirmed cause: drift, over-engineering, or a wrong approach.

## Recovery

### `error-ignored`
S->J · recovery · fix: model
- **What:** a call failed and the next message claims success.
- **Confirm:** read the failure and the claim. Dismiss the candidate if the failure was
  irrelevant to the claim, for example an optional check.
- **Fix:** add the rule "report failures honestly; re-run before claiming success" to the
  instructions file.

### `give-up`
S->J · recovery · fix: model
- **What:** the session stopped after a single failure.
- **Confirm:** judge whether the failure was recoverable (a typo, or a path that exists
  elsewhere). Dismiss the candidate if the environment genuinely blocked the task.
- **Fix:** add the rule "on failure, diagnose and try one alternative before stopping" to the
  instructions file.

## Alignment

### `unverified-completion`
S->J · alignment · fix: harness
- **What:** the agent claimed completion with no test or build after its last change.
- **Confirm:** check whether the change needed verification. Documentation changes may not.
- **Fix:** require a verification command before a completion claim in the instructions
  file.

### `premature-stop`
S->J · alignment · fix: model
- **What:** the session ended on a stated next step that never ran.
- **Confirm:** check whether the step was needed for the ask.
- **Fix:** add the rule "finish the stated plan or say why not" to the instructions file.
  With open models, check for an `empty-turn` or `tool-text-leak` at the end.

### `scope-creep`
S->J · alignment · fix: harness
- **What:** the ask was to review or explain, but files changed.
- **Confirm:** re-read the ask; implicit permission counts.
- **Fix:** state read-only intent explicitly. Use a plan/read-only mode where the harness has
  one.

### `drift-candidate`
S->J · alignment · fix: harness
- **What:** a large share of a turn went to things the ask never referenced, or the calls
  right before a user interrupt.
- **Confirm:** read the run's paths or commands against the ask. Necessary investigation (a
  dependency of a referenced file) is a dismissal. A rabbit hole, such as reading a
  package's source when the docs would do, is a confirmation.
- **Fix:** give the ask a scope: name the files or the expected approach. Add the rule "prefer
  docs over source; ask before deep dives" to the instructions file.

### `false-premise`
J · alignment · fix: model
- **What:** the agent acted on an assumption it never checked. This is the largest single
  cause of failure in the literature.
- **Confirm:** walk back from the first failure to the first unverified assumption.
- **Fix:** add the rule "verify assumptions (paths, versions, environment) before acting" to
  the instructions file, naming the specific assumption.

### `spec-neglect`
J · alignment · fix: harness
- **What:** a stated requirement of the ask was dropped.
- **Confirm:** list the ask's requirements and mark which ones were met.
- **Fix:** put requirements in a checklist the agent must tick. Tell it to restate the
  requirements before starting.

### `misread-output`
J · alignment · fix: model
- **What:** a tool result contradicted the agent's next action.
- **Confirm:** quote the output and the action that ignored it.
- **Fix:** model-side; compare models. For a harness fix, reduce output noise (quieter
  commands) so the signal is visible.

### `ignored-own-warning`
J · alignment · fix: model
- **What:** the agent stated a hazard or constraint, then acted against it.
- **Confirm:** quote both the warning and the violating action.
- **Fix:** state the constraint as a hard rule in the instructions file. Structural risks
  (for example, benchmarking the server the agent runs on) need an environment change, not a
  prompt.

### `role-drift`
J · alignment · fix: harness
- **What:** an orchestrator did its delegate's work in parallel with the delegate.
- **Confirm:** the parent's calls in the child's run window touch the child's task.
- **Fix:** the orchestration skill should say "while a delegate runs, only observe; steer
  through the dispatch tool".

## Environment

### `hook-block`
S · environment · fix: harness
- **What:** a hook rejected the call after the model emitted it.
- **Confirm:** the result names the hook's rule. Check whether that rule is already in the
  instructions file.
- **Fix:** Claude Code: state the rule in `CLAUDE.md` and keep the hook. pi: state the rule
  in `AGENTS.md`; enforcement is a `tool_call` extension.

### `user-rejected`
S · environment · fix: none
- **What:** the user declined a proposed call.
- **Confirm:** read what was proposed.
- **Fix:** an approach problem; clarify in the ask what not to attempt.

### `policy-denied`
S · environment · fix: harness
- **What:** a permission policy, a worktree isolation rule or an extension blocked the call.
  No human saw it.
- **Confirm:** check whether the call was reasonable.
- **Fix:** allowlist it, or state the boundary in the instructions file.

### `tool-error-structured`
S · environment · fix: env
- **What:** a tool returned `{"ok": false, "errors": …}`.
- **Confirm:** read the errors list; the input usually broke the tool's own rules.
- **Fix:** fix the input pattern. If one tool dominates, its documentation is unclear.

### `missing-tool`
S · environment · fix: env
- **What:** a command was not found.
- **Confirm:** check whether it is installed on this machine.
- **Fix:** install it, or tell the agent which tool to use instead.

### `permission-denied`
S · environment · fix: env
- **What:** the write target was not writable.
- **Confirm:** check the path.
- **Fix:** name the writable locations in the instructions file.

### `rate-limit`
S · environment · fix: env
- **What:** the provider or a tool throttled the request.
- **Confirm:** check how often it happened in the session.
- **Fix:** back off, or route to another provider.

### `api-error`
S · environment · fix: env
- **What:** a model or tool endpoint returned a server error, or finished for an unexpected
  reason (`content_filter`).
- **Confirm:** check whether it was transient.
- **Fix:** retry policy, or a provider check.

### `mcp-error`
S · environment · fix: env
- **What:** an MCP server failed (errors, or no transport).
- **Confirm:** check the server, not the prompt.
- **Fix:** restart or fix the MCP server.

### `context-overflow`
S · environment · fix: harness
- **What:** the prompt plus the reserved output tokens exceeded the context window.
- **Confirm:** read the error. Prompt tokens plus `maxTokens` exceed the window; pi then
  compacts and retries.
- **Fix:** lower the model's `maxTokens` in pi's `models.json` (32K reserved on a 131K window
  leaves 98K), or compact earlier. A large reservation on a small window is the common cause.

### `auth`
S · environment · fix: env
- **What:** the provider sign-in is missing or expired.
- **Confirm:** the error names `401`, `invalid_grant`, a login or OAuth.
- **Fix:** re-authenticate (`pi auth`, `/login`). Not a model problem.

### `network`
S · environment · fix: env
- **What:** the model endpoint was unreachable or too slow (`Request timed out`, connection
  refused).
- **Confirm:** check the server's health at that time, and whether something else was loading
  it (see `resource-interference`).
- **Fix:** server capacity or restarts; keep benchmarks and other heavy jobs off the server
  the agent uses.

### `resource-interference`
S->J · environment · fix: harness
- **What:** a model call failed while another session in the same project ran a long
  command.
- **Confirm:** open the other session (`sk show <sid> --mode tools`) and check that the
  command used the shared resource (the model server, GPU, a lock). Dismiss the candidate if
  the timing is coincidental.
- **Fix:** do not run heavy jobs on the server the agent depends on; serialize them, or point
  the agent at a separate endpoint during the job.

## Generic

### `not-a-repo`
S · generic · fix: none
- **What:** a git command ran outside a repository.
- **Confirm:** check the working directory.
- **Fix:** state the repo layout in the instructions file if this recurs.

### `not-found`
S · generic · fix: none
- **What:** a path did not exist.
- **Confirm:** check whether it was a false premise (an assumed path).
- **Fix:** tell the agent to list before reading. A recurring path belongs in the
  instructions file.

### `timeout`
S · generic · fix: none
- **What:** a command hit its timeout.
- **Confirm:** check whether the timeout was realistic.
- **Fix:** raise the timeout, or split the work.

### `exit-code`
S · generic · fix: none
- **What:** a command failed with a non-zero exit code.
- **Confirm:** read the output. This is often a genuine failure that needs no systemic fix.
- **Fix:** usually none; say so rather than inventing one.
````

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parity -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/session-forensics/reference/failure-classes.md tests/test_parity.py
git commit -m "Add the failure-class reference the post-mortem skill loads"
```

---

### Task 17: The post-mortem skill, the fleet skill, and the README

**Files:**
- Modify: `skills/session-forensics/SKILL.md` (full rewrite)
- Modify: `skills/error-patterns/SKILL.md`
- Modify: `README.md` (the Commands table and the layer table)

**Interfaces:**
- Consumes the `sk forensics --json` tables `findings`, `focus`, `timeline` and `health`
  (Task 14), plus `sk findings`, `sk index --finding` and `sk errors --group-by
  model|harness` (Task 15), and the reference file (Task 16).

There is no automated test for skill text. Step 4 is a manual check that runs the skill's
commands against the dry-run sessions.

- [ ] **Step 1: Rewrite `skills/session-forensics/SKILL.md`**

````markdown
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
````

- [ ] **Step 2: Update `skills/error-patterns/SKILL.md`**

1. Replace the `## Tool` section's `SK=/mnt/c/...` block, and the sentence after it, with
   the same `$SK` resolution block used in `session-forensics` (Step 1 above).
2. After `### 3. Drill into the top clusters` (and its code block), insert:

````markdown
### 3b. Findings, not just failed calls

Loops, drift, degeneration and stalls are not failed tool calls, so `sk errors` cannot see
them. `sk findings` reports detector findings as **rates** (sessions affected out of sessions
in the group):

```bash
$SK findings --group-by kind --since 30d                  # what goes wrong most
$SK findings --group-by model --kind degeneration         # is it one model?
$SK findings --group-by harness                           # Claude Code vs pi
$SK errors --group-by model                               # failed calls per model, with reach
```

Compare rates, never raw counts: the most-used model always has the most findings. A group
with fewer than 10 sessions has no meaningful rate yet, so say so.
````

3. In `## Fix types, in order of preference`, change item 1 to:
   `1. **An instructions-file rule** — \`CLAUDE.md\` for Claude Code, \`AGENTS.md\` for pi (pi
   also reads \`CLAUDE.md\`). Best for anything self-inflicted and predictable.`
4. Add these rows to the `## Interpreting classes` table:

```markdown
| `edit-mismatch` | Editing from a stale view | "Re-read before editing" rule |
| `invalid-args` / `unknown-tool` | Model misreads its tool schema | A usage example in the instructions file; compare models |
| `args-dropped` / `tool-text-leak` | Serving-stack tool-call parser | vLLM parser/chat template for that model |
| `truncated-call` / `context-overflow` | Token limits | `maxTokens` / window settings in pi's `models.json` |
| `auth` / `network` | Provider or model server | Re-authenticate; server health and load |
| `tool-error-structured` | A tool's own rules rejected the input | Read the errors list; fix the input pattern |
```

5. In the boundaries table, change the `session-forensics` row's ask to
   `"Why did *this* session fail / loop / stall / drift?" (a post-mortem)`.

- [ ] **Step 3: Update `README.md`**

In the layer table, change the layer-2 row's command cell to `` `sk errors`, `sk findings`,
`sk doctor` ``. In the Commands table:
- Change the `index` row's flags to `--since --project --source --state --finding
  --subagents`.
- Change the `errors` row's group-by list to
  `class\|tool\|signature\|session\|model\|harness`.
- Change the `forensics <sid>` purpose to
  `Layer 3 post-mortem: tiered findings, focus profile, anchored timeline, health`.
- Add the row:
  `| \`findings\` | Layer 2 detector findings as rates; \`--group-by kind\|model\|harness\|project\|session --tier --family --kind\` |`

- [ ] **Step 4: Check the skills' commands against the dry-run sessions**

Run each command the updated skills name, against the pi test corpora, and confirm it runs
and returns the tables the skill describes:

```bash
export PYTHONPATH=. CLAUDE_DIR=/nonexistent
PI_DIR=$PWD/test/pi-mother python3 -m sessionkit index --source pi --finding degeneration --since 2026-09-01
PI_DIR=$PWD/test/pi-mother python3 -m sessionkit forensics 01a0db2c --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(k for k in d if isinstance(d[k], list)))"
PI_DIR=$PWD/test/pi-prana python3 -m sessionkit findings --group-by model --since 2026-09-01
PI_DIR=$PWD/test/pi-prana python3 -m sessionkit children 01a0db1d
```

Expected:
- the `index` lists `01a0db2c`;
- the `forensics` key list includes `findings`, `focus`, `health` and `timeline`;
- `findings` prints rates, with the small-sample note;
- `children` shows the async dispatch resolved to `01a0db48`.

- [ ] **Step 5: Commit**

```bash
git add skills/session-forensics/SKILL.md skills/error-patterns/SKILL.md README.md
git commit -m "Turn session-forensics into a tiered post-mortem; teach error-patterns rates"
```

---

### Task 18: Validation — corpus acceptance, dry-run regression, real pi sessions

**Files:**
- No files are created or modified, unless validation reveals a defect. A defect is fixed in
  the owning module, with a new inline-fixture test that reproduces its shape.
- Scratch (not committed): `<scratchpad>/acceptance.py`

**Interfaces:** none new.

- [ ] **Step 1: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 2: Measure corpus acceptance (spec §7.5)**

Write `<scratchpad>/acceptance.py`:

```python
"""Corpus acceptance metrics for the post-mortem work (spec §7.5). Scratch; not committed."""

from collections import Counter

from sessionkit import corpus, harness


def main() -> None:
    sessions = corpus.load().sessions
    n = len(sessions)

    def share(kind: str) -> str:
        hit = sum(1 for e in sessions if any(a.kind == kind for a in e.anomalies))
        return f"{hit}/{n} ({100 * hit / n:.1f}%)" if n else "n/a"

    other = sum(1 for e in sessions for t in e.session.tools
                if t.is_error and (t.err_class or "other") == "other")
    thrash = real = 0
    for e in sessions:
        failing = {p for t in e.session.tools if t.is_error
                   and harness.role(e.session.harness, t.name) in ("edit", "write")
                   for p in harness.call_paths(e.session.harness, t)}
        for a in e.anomalies:
            if a.kind == "file-thrash":
                thrash += 1
                real += a.detail in failing
    pi_bash = [t for e in sessions if e.session.harness == "pi"
               for t in e.session.tools if t.name == "bash" and t.is_error]
    print("sessions:", n)
    print("stall share:", share("stall"), "(target < 10%)")
    print("other bucket:", other, "(target <= 230)")
    print("file-thrash with a real edit failure:", f"{real}/{thrash}", "(target: majority)")
    print("read-only-spiral share:", share("read-only-spiral"), "(target <= 5%)")
    classified = sum(1 for t in pi_bash if t.err_class not in ("", "other"))
    print("pi bash failures classified:", f"{classified}/{len(pi_bash)}", "(target: all)")
    print(Counter(a.kind for e in sessions for a in e.anomalies).most_common())


if __name__ == "__main__":
    main()
```

Run it against the real corpus, then against the pi test corpora (with Claude Code excluded):

```bash
PYTHONPATH=. python3 <scratchpad>/acceptance.py
for d in pi-prana pi-mother; do
  CLAUDE_DIR=/nonexistent PI_DIR=$PWD/test/$d PYTHONPATH=. python3 <scratchpad>/acceptance.py
done
```

Record every number for the PR description. If a target is missed:
- **`read-only-spiral` > 5%:** raise `THRESHOLDS["read_only_spiral"]` by 2 and re-measure,
  until it is at most 5%. Update `ReadOnlySpiralTest` so it builds that many calls, and
  re-run the suite.
- **`stall` ≥ 10%, `other` > 230, or thrash not a majority:** list the top offenders.
  - For `stall`: the tool names on stalled calls.
  - For `other`: `sk errors --group-by signature` filtered to `other`.
  - For thrash: the thrash paths.
  **Stop and report them to the user** before changing any rule. A rule change here is a
  spec change.

- [ ] **Step 3: Dry-run regression (spec §7.7, §A.4)**

```bash
export PYTHONPATH=. CLAUDE_DIR=/nonexistent
PI_DIR=$PWD/test/pi-prana python3 -m sessionkit forensics 01a0db1d
PI_DIR=$PWD/test/pi-prana python3 -m sessionkit index --source pi --subagents include --since 2026-09-01
PI_DIR=$PWD/test/pi-mother python3 -m sessionkit forensics 01a0db2c
PI_DIR=$PWD/test/pi-prana python3 -c "
from pathlib import Path; from sessionkit.parse_pi import parse_file
import glob
for f in glob.glob('test/pi-prana/sessions/*/*01a0db1d*.jsonl'): print(parse_file(Path(f), 'pi').malformed_lines)"
```

Expected for **`01a0db1d`**:
- **State:** `agent-unresponsive`.
- **Findings include:**
  - `context-overflow` (line 57);
  - `network` (line 79);
  - `resource-interference` (candidate, line 79);
  - `user-interrupt` (lines 83, 84);
  - a `drift-candidate` citing calls before line 83.
- **No `stall` finding.**
- **`index`** lists `01a0db48` with parent `01a0db1d` and agent `developer-qwen38`.
- **The malformed-line count** printed is `0`.

Expected for **`01a0db2c`**:
- **State:** `aborted`.
- **Findings include:**
  - `degeneration` (line 60);
  - `generation-stall` (line 60);
  - `user-interrupt` (lines 33, 34);
  - a `drift-candidate` citing calls at or before line 33.

If any expectation fails:
1. Use superpowers:systematic-debugging to find the root cause.
2. Fix the owning module.
3. Add an inline-fixture test that reproduces the shape (never copy `test/` text).
4. Re-run Steps 1 and 3.

- [ ] **Step 4: Real pi sessions on local models (spec §7.6) — ask the user first**

**Ask the user before this step.** It runs pi against `prana`, and the truncation case
needs a model entry added to `~/.pi/agent/models.json`. Run every case in a throwaway clone,
never this worktree:

```bash
git clone --quiet "$PWD" <scratchpad>/pi-scratch && cd <scratchpad>/pi-scratch
```

1. **Truncation (needs approval for the config change).** Add a copy of the default prana
   model entry with `"maxTokens": 256` and a new id such as `qwen3.8-27b-nvfp4-lowmax`, then
   run:
   `pi -p --model prana/qwen3.8-27b-nvfp4-lowmax "Write a 300-line Python module that
   implements a CLI todo app, in one file."`
2. **Long context.** Run this to try to trigger compaction and, on qwen, tool-call leakage:
   `pi -p "Read every file under sessionkit/ and tests/, then summarise each function in one
   line."`
3. **Esc abort.** This one is interactive, so the user runs it: start `pi`, give it any task,
   and press Esc mid-turn.

Then, for each new session:
- run `PYTHONPATH=<worktree> python3 -m sessionkit forensics <sid>` from the worktree;
- confirm the expected finding: `output-truncated` or `truncated-call`; `compaction` in
  Health, plus any `tool-text-leak`; `aborted` or `user-interrupt`;
- fold any surprise back as in Step 3.

Remove the temporary model entry afterwards, with the user's agreement.

A case that doesn't reproduce is a finding in its own right. For example, pi may never
compact within one `-p` run. Report it plainly instead of forcing it.

- [ ] **Step 5: Report**

Give the user:
- the acceptance numbers, before and after;
- the dry-run regression results;
- what the real pi sessions showed;
- any thresholds tuned, and any fixes committed along the way.
