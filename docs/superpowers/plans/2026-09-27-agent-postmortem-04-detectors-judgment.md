# Checkpoint 4: Candidate and cross-session detectors

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this checkpoint task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Part of:** the agent post-mortem plan. Tracker: `2026-09-27-agent-postmortem-00-tracker.md`. Read its **Global Constraints**, **Spec deviations** and **Review Focus** before starting; they apply to every task here.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**Goal:** The recovery and alignment candidates, the focus profile, drift candidates, resource interference, and the registry completeness tests.

**Starts from:** Checkpoint 3 is complete: its tasks are ticked in the tracker and the full suite passes.

**Done when:** every task below is committed; `PYTHONPATH=. python3 -m unittest discover -s tests -t .` passes; and the tracker marks each task done with its commit.

---

### Task 11: Recovery and alignment candidates

**Files:**
- Create: `sessionkit/detectors/recovery.py` and `sessionkit/detectors/alignment.py`.
- Modify: `sessionkit/classify.py`. Add `tail_text_signal`; `derive_tail_signal` now uses it
  and behaves the same.
- Modify: `sessionkit/taxonomy.py` (`FINDING_KINDS`).
- Modify: `sessionkit/detectors/__init__.py` (the registry).
- Test: `tests/test_detectors_recovery.py` and `tests/test_detectors_alignment.py` (both new).

**Interfaces:**
- Consumes:
  - `Message.claims_done` and `ParsedSession.final_text` (Tasks 4 and 5);
  - `classify.trailing_errors` (Task 2);
  - `harness.role`, `command_of`, `is_verify_command` and `is_interrupt_marker`.
- Produces:
  - `classify.tail_text_signal(text) -> str`;
  - `alignment.TurnSpan(ask, calls)` and `alignment.user_turns(session) -> list[TurnSpan]`,
    which Task 12 uses;
  - the S->J finding kinds `error-ignored`, `give-up`, `unverified-completion`,
    `premature-stop` and `scope-creep`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detectors_recovery.py`:

```python
"""Recovery candidates: failures the agent ignored or gave up on."""

from __future__ import annotations

import unittest

from sessionkit.detectors import recovery
from tests import detector_fixtures as df


class ErrorIgnoredTest(unittest.TestCase):
    def test_failure_then_a_success_claim(self) -> None:
        s = df.session(tools=[df.call(2, is_error=True, err_class="exit-code", result_line=3)],
                       messages=[df.msg(4, text="All tests pass.", claims_done=True)])
        self.assertEqual([a.lines for a in recovery.error_ignored(s)], [[2, 4]])

    def test_a_retry_before_the_claim_is_not_ignoring(self) -> None:
        s = df.session(tools=[df.call(2, is_error=True, err_class="exit-code", result_line=3),
                              df.call(4, result_line=5)],
                       messages=[df.msg(6, text="All tests pass.", claims_done=True)])
        self.assertEqual(recovery.error_ignored(s), [])

    def test_a_user_abort_is_not_the_agents_failure(self) -> None:
        s = df.session(tools=[df.call(2, is_error=True, err_class="aborted", result_line=3)],
                       messages=[df.msg(4, text="Done.", claims_done=True)])
        self.assertEqual(recovery.error_ignored(s), [])


class GiveUpTest(unittest.TestCase):
    def _session(self, final_text: str, errors: int = 1):
        tools = [df.call(i, is_error=True, err_class="exit-code", result_line=i)
                 for i in range(2, 2 + errors)]
        final = df.msg(10, text=final_text)
        return df.session(tools=tools, messages=[df.msg(1, "user", text="go"), final],
                          final_text=final_text, end_state="complete")

    def test_one_failure_then_an_apology(self) -> None:
        s = self._session("Sorry, I cannot get this command to work.")
        self.assertEqual([a.kind for a in recovery.give_up(s)], ["give-up"])

    def test_a_cascade_is_not_giving_up_after_one_failure(self) -> None:
        self.assertEqual(recovery.give_up(self._session("Sorry.", errors=3)), [])

    def test_a_completion_claim_is_not_giving_up(self) -> None:
        self.assertEqual(recovery.give_up(self._session("Done, it works now.")), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

Create `tests/test_detectors_alignment.py`:

```python
"""Alignment candidates: did the work match the ask?"""

from __future__ import annotations

import unittest

from sessionkit.detectors import alignment
from tests import detector_fixtures as df


class ScopeCreepTest(unittest.TestCase):
    def _session(self, ask: str):
        return df.session(messages=[df.msg(1, "user", text=ask)],
                          tools=[df.call(2, "read", {"path": "/a.py"}),
                                 df.call(3, "edit", {"path": "/a.py"})])

    def test_review_ask_with_edits(self) -> None:
        s = self._session("review the parser, don't change anything")
        self.assertEqual([a.lines for a in alignment.scope_creep(s)], [[1, 3]])

    def test_an_implement_ask_is_not_scope_creep(self) -> None:
        self.assertEqual(alignment.scope_creep(self._session("review and fix the parser")), [])


class UnverifiedCompletionTest(unittest.TestCase):
    def _session(self, *commands: str):
        tools = [df.call(2, "edit", {"path": "/a.py"})]
        tools += [df.call(3 + i, "bash", {"command": c}) for i, c in enumerate(commands)]
        final = df.msg(20, text="All tests pass.", claims_done=True)
        return df.session(tools=tools, messages=[df.msg(1, "user", text="fix it"), final],
                          final_text="All tests pass.")

    def test_claim_with_no_verification(self) -> None:
        found = alignment.unverified_completion(self._session("git diff"))
        self.assertEqual([a.lines for a in found], [[2, 20]])

    def test_claim_after_running_tests(self) -> None:
        self.assertEqual(alignment.unverified_completion(self._session("pytest -q")), [])

    def test_no_changes_means_nothing_to_verify(self) -> None:
        s = df.session(messages=[df.msg(5, text="Done.", claims_done=True)], final_text="Done.")
        self.assertEqual(alignment.unverified_completion(s), [])


class PrematureStopTest(unittest.TestCase):
    def test_ends_on_a_stated_next_step(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go"),
                                 df.msg(4, text="I'll run the tests next.")],
                       final_text="I'll run the tests next.", end_state="complete")
        self.assertEqual([a.lines for a in alignment.premature_stop(s)], [[4]])

    def test_a_finished_answer_is_not_premature(self) -> None:
        s = df.session(messages=[df.msg(4, text="Finished; all tests pass.")],
                       final_text="Finished; all tests pass.", end_state="complete")
        self.assertEqual(alignment.premature_stop(s), [])


class UserTurnsTest(unittest.TestCase):
    def test_turns_split_on_real_asks_only(self) -> None:
        s = df.session("claude-code", messages=[
            df.msg(1, "user", text="first"),
            df.msg(4, "user", text="[Request interrupted by user]"),
            df.msg(6, "user", text="second")],
            tools=[df.call(2, "Read"), df.call(5, "Read"), df.call(7, "Read")])
        turns = alignment.user_turns(s)
        self.assertEqual([(t.ask.line, [c.line for c in t.calls]) for t in turns],
                         [(1, [2, 5]), (6, [7])])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_detectors_recovery tests.test_detectors_alignment -v`
Expected: FAIL with `ImportError: cannot import name 'recovery'`.

- [ ] **Step 3: Split out `tail_text_signal` in `sessionkit/classify.py`**

Add this function directly after `derive_tail_signal`:

```python
def tail_text_signal(text: str) -> str:
    """Classify a final message's text alone: ``apology``, ``completion_marker``,
    ``next_step_stated`` or ``neutral``."""
    if _APOLOGY_RE.search(text):
        return "apology"
    if _COMPLETION_RE.search(text):
        return "completion_marker"
    if _NEXT_STEP_RE.search(text):
        return "next_step_stated"
    return "neutral"
```

In `derive_tail_signal`, replace the last seven lines (from `if _APOLOGY_RE.search(text):`
through `return "neutral"`) with:

```python
    return tail_text_signal(text)
```

- [ ] **Step 4: Add the finding kinds**

Append to `FINDING_KINDS` in `sessionkit/taxonomy.py`:

```python
    _kind("error-ignored", "recovery", TIER_CANDIDATE, "model",
          "A tool failed and the next message claims success — confirm whether the failure "
          "was actually handled."),
    _kind("give-up", "recovery", TIER_CANDIDATE, "model",
          "The session stopped after a single failure that may have been recoverable."),
    _kind("unverified-completion", "alignment", TIER_CANDIDATE, "harness",
          "The agent claimed completion without running tests or a build after its last "
          "change. Require verification in the instructions file."),
    _kind("premature-stop", "alignment", TIER_CANDIDATE, "model",
          "The session ends on a stated next step that never happened."),
    _kind("scope-creep", "alignment", TIER_CANDIDATE, "harness",
          "The ask was to review or explain, but files were changed. State read-only intent "
          "explicitly in the ask or the instructions file."),
```

- [ ] **Step 5: Create `sessionkit/detectors/recovery.py`**

```python
"""Recovery candidates (tier S->J): how the agent handled its own failures."""

from __future__ import annotations

from sessionkit.classify import tail_text_signal, trailing_errors
from sessionkit.detectors.base import Anomaly
from sessionkit.parse import ParsedSession, ToolCall

#: Failures the agent is not expected to recover from: the user or a sibling call caused them.
_NOT_THE_AGENTS = ("aborted", "sibling-cascade", "user-rejected")


def error_ignored(session: ParsedSession) -> list[Anomaly]:
    """A failed call, then a next message claiming success with no retry in between."""
    assistants = [m for m in session.messages if m.role == "assistant"]
    out: list[Anomaly] = []
    for call in session.tools:
        if not call.is_error or call.err_class in _NOT_THE_AGENTS:
            continue
        after = call.result_line or call.line
        claim = next((m for m in assistants if m.line > after), None)
        if claim is None or not claim.claims_done or _retried(session.tools, call, claim.line):
            continue
        out.append(Anomaly("error-ignored",
                           f"{call.name} failed ({call.err_class or 'error'}); the next message "
                           "claims success", 1, [call.line, claim.line]))
    return out


def _retried(tools: list[ToolCall], failed: ToolCall, until: int) -> bool:
    return any(c is not failed and c.name == failed.name and failed.line < c.line <= until
               for c in tools)


def _lone_final_failure(session: ParsedSession) -> ToolCall | None:
    """The session's last call, if it failed on its own (not in a cascade) and it was the
    agent's failure."""
    if not session.tools:
        return None
    last = session.tools[-1]
    if not last.is_error or last.err_class in _NOT_THE_AGENTS:
        return None
    return last if trailing_errors(session.tools) == 1 else None


def give_up(session: ParsedSession) -> list[Anomaly]:
    """One failure, then the session ends with an apology or without comment."""
    failed = _lone_final_failure(session)
    if failed is None or not session.messages or session.end_state.startswith("interrupted"):
        return []
    final = session.messages[-1]
    if final.role != "assistant" or final.line <= (failed.result_line or failed.line):
        return []
    if tail_text_signal(session.final_text or final.preview) not in ("apology", "neutral"):
        return []
    return [Anomaly("give-up",
                    f"stopped after a single {failed.name} failure "
                    f"({failed.err_class or 'error'})", 1, [failed.line, final.line])]
```

- [ ] **Step 6: Create `sessionkit/detectors/alignment.py`**

```python
"""Alignment candidates (tier S->J): did the work match what the user asked for?"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from sessionkit.classify import tail_text_signal
from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.features import claims_done
from sessionkit.harness import command_of, is_interrupt_marker, is_verify_command, role
from sessionkit.parse import Message, ParsedSession, ToolCall

_REVIEW = re.compile(
    r"\b(?:review|explain|analy[sz]e|audit|summari[sz]e|describe|what does|why does|"
    r"read-only|don'?t (?:change|edit|modify)|do not (?:change|edit|modify))\b", re.I)
_IMPLEMENT = re.compile(
    r"\b(?:fix|implement|add|change|refactor|update|write|create|build|make|edit|modify|"
    r"remove|delete|rename)\b", re.I)
_NEGATED = re.compile(r"\b(?:don'?t|do not|never|without)\s+\w+", re.I)
_CHANGE_ROLES = ("edit", "write")


@dataclass
class TurnSpan:
    """One user ask and the tool calls made before the next ask."""

    ask: Message
    calls: list[ToolCall]


def user_turns(session: ParsedSession) -> list[TurnSpan]:
    """Split a session at each real user ask (Claude Code's interrupt markers are not asks)."""
    asks = [m for m in session.messages if m.role == "user" and m.text_len > 0
            and not is_interrupt_marker(m.preview)]
    spans = []
    for index, ask in enumerate(asks):
        end = asks[index + 1].line if index + 1 < len(asks) else sys.maxsize
        spans.append(TurnSpan(ask, [c for c in session.tools if ask.line < c.line < end]))
    return spans


def scope_creep(session: ParsedSession) -> list[Anomaly]:
    """The ask was to review or explain, and the turn changed files anyway."""
    out: list[Anomaly] = []
    for turn in user_turns(session):
        ask = turn.ask.preview
        if not _REVIEW.search(ask) or _IMPLEMENT.search(_NEGATED.sub("", ask)):
            continue
        changes = [c.line for c in turn.calls
                   if role(session.harness, c.name) in _CHANGE_ROLES and not c.is_error]
        if changes:
            out.append(Anomaly("scope-creep",
                               "the ask was to review or explain, but the turn changed files",
                               len(changes), [turn.ask.line] + changes[:MAX_LINES - 1]))
    return out


def unverified_completion(session: ParsedSession) -> list[Anomaly]:
    """A final completion claim with no test/build command after the last successful change."""
    if not session.messages or session.messages[-1].role != "assistant":
        return []
    final = session.messages[-1]
    if not claims_done(session.final_text or final.preview):
        return []
    changes = [i for i, c in enumerate(session.tools)
               if role(session.harness, c.name) in _CHANGE_ROLES and not c.is_error]
    if not changes:
        return []
    last_change = changes[-1]
    later = session.tools[last_change + 1:]
    if any(role(session.harness, c.name) == "exec" and is_verify_command(command_of(c))
           for c in later):
        return []
    return [Anomaly("unverified-completion",
                    "claims the work is done, but no test or build ran after the last change",
                    1, [session.tools[last_change].line, final.line])]


def premature_stop(session: ParsedSession) -> list[Anomaly]:
    """A session that ended normally on a stated next step."""
    if session.end_state != "complete" or not session.messages:
        return []
    final = session.messages[-1]
    if tail_text_signal(session.final_text or final.preview) != "next_step_stated":
        return []
    return [Anomaly("premature-stop", "the session ends on a stated next step", 1,
                    [final.line])]
```

- [ ] **Step 7: Register them**

In `sessionkit/detectors/__init__.py`, change the import to
`from sessionkit.detectors import alignment, generation, lifecycle, loops, recovery,
tool_protocol`, then append to `DETECTORS`:

```python
    recovery.error_ignored,
    recovery.give_up,
    alignment.scope_creep,
    alignment.unverified_completion,
    alignment.premature_stop,
```

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/detectors sessionkit/classify.py sessionkit/taxonomy.py \
  tests/test_detectors_recovery.py tests/test_detectors_alignment.py
git commit -m "Flag ignored errors, give-ups, unverified completion and scope creep"
```

---

### Task 12: Focus profile and drift candidates

**Files:**
- Modify: `sessionkit/detectors/alignment.py`. Add `Run`, `TurnProfile`, `focus_profile`
  and `drift_candidates`.
- Modify: `sessionkit/taxonomy.py`. Add to `THRESHOLDS`; add to `FINDING_KINDS`, including
  the J-tier kinds.
- Modify: `sessionkit/detectors/__init__.py` (the registry).
- Test: `tests/test_detectors_alignment.py` (new class `FocusProfileTest`).

**Interfaces:**
- Consumes:
  - `alignment.user_turns` (Task 11) and `lifecycle.user_interrupt` (Task 10);
  - `harness.run_key` and `call_paths` (Task 2);
  - `Message.refs` (Tasks 4 and 5).
- Produces:
  - `alignment.Run(key, lines, paths, referenced)`;
  - `alignment.TurnProfile(ask_line, ask_preview, refs, calls, runs)`;
  - `alignment.focus_profile(session) -> list[TurnProfile]`, which Task 14 uses;
  - the finding kind `drift-candidate`;
  - the J-tier kinds `false-premise`, `spec-neglect`, `misread-output`,
    `ignored-own-warning` and `role-drift`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_detectors_alignment.py` (before the `if __name__` line):

```python
class FocusProfileTest(unittest.TestCase):
    ASK = "fix the bug in sessionkit/parse.py"

    def _session(self, tools, ask: str = ASK, **kw):
        refs = ["sessionkit/parse.py"] if "sessionkit/parse.py" in ask else []
        return df.session(messages=[df.msg(1, "user", text=ask, refs=refs)], tools=tools, **kw)

    def test_consecutive_calls_on_one_key_form_one_run(self) -> None:
        s = self._session([df.call(2, "read", {"path": "/repo/sessionkit/parse.py"}),
                           df.call(3, "read", {"path": "/repo/sessionkit/corpus.py"}),
                           df.call(4, "bash", {"command": "rg foo"})])
        runs = alignment.focus_profile(s)[0].runs
        self.assertEqual([(r.key, r.lines) for r in runs],
                         [("dir:/repo/sessionkit", [2, 3]), ("exec:rg", [4])])
        self.assertTrue(runs[0].referenced)

    def test_a_large_unreferenced_run_is_a_drift_candidate(self) -> None:
        tools = [df.call(2, "read", {"path": "/repo/sessionkit/parse.py"}),
                 df.call(3, "edit", {"path": "/repo/sessionkit/parse.py"})]
        tools += [df.call(i, "read", {"path": f"/usr/lib/node_modules/pkg/dist/{i}.js"})
                  for i in range(4, 12)]
        drift = [a for a in alignment.drift_candidates(self._session(tools))]
        self.assertEqual([a.lines for a in drift], [list(range(4, 12))])

    def test_asks_without_refs_produce_no_ref_based_candidates(self) -> None:
        tools = [df.call(i, "read", {"path": f"/usr/lib/pkg/{i}.js"}) for i in range(2, 12)]
        s = self._session(tools, ask="make the profiles switchable")
        self.assertEqual(alignment.drift_candidates(s), [])

    def test_a_user_interrupt_marks_the_calls_before_it(self) -> None:
        tools = [df.call(i, "bash", {"command": f"rg x{i}"}) for i in range(2, 32, 2)]
        s = df.session(messages=[df.msg(1, "user", text="set up two profiles"),
                                 df.msg(33, stop="aborted"),
                                 df.msg(34, "user", text="just write a script")], tools=tools)
        drift = alignment.drift_candidates(s)
        self.assertEqual([a.lines for a in drift], [[16, 18, 20, 22, 24, 26, 28, 30]])

    def test_judgment_kinds_are_registered(self) -> None:
        from sessionkit import taxonomy
        for name in ("false-premise", "spec-neglect", "misread-output",
                     "ignored-own-warning", "role-drift"):
            with self.subTest(name):
                self.assertEqual(taxonomy.lookup(name).tier, taxonomy.TIER_J)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_detectors_alignment -v`
Expected: FAIL with `AttributeError: module 'sessionkit.detectors.alignment' has no attribute
'focus_profile'`.

- [ ] **Step 3: Thresholds and finding kinds**

Add to `THRESHOLDS`:

```python
    "drift_min_calls": 8,         # an unreferenced run needs at least this many calls ...
    "drift_min_share": 0.4,       # ... and this share of its turn's calls
    "interrupt_drift_calls": 8,   # calls cited before a user interrupt
```

Append to `FINDING_KINDS`:

```python
    _kind("drift-candidate", "alignment", TIER_CANDIDATE, "harness",
          "A large share of a turn went to files or commands the ask never referenced, or the "
          "user interrupted it — confirm whether it was needed investigation or a detour."),
    _kind("false-premise", "alignment", TIER_J, "model",
          "The agent acted on an assumption it never checked. Walk back from the first "
          "failure to the first unverified assumption."),
    _kind("spec-neglect", "alignment", TIER_J, "harness",
          "A stated requirement of the ask was dropped. List the requirements and mark which "
          "were met."),
    _kind("misread-output", "alignment", TIER_J, "model",
          "A tool result contradicted the agent's next action — it misread or ignored what "
          "the tool said."),
    _kind("ignored-own-warning", "alignment", TIER_J, "model",
          "The agent stated a hazard or constraint, then acted against it."),
    _kind("role-drift", "alignment", TIER_J, "harness",
          "An orchestrator did its delegate's work in parallel with the delegate, duplicating "
          "effort and competing for shared resources."),
```

- [ ] **Step 4: Add the focus profile to `sessionkit/detectors/alignment.py`**

Add the imports `import posixpath`, `from sessionkit.detectors.lifecycle import
user_interrupt`, and `from sessionkit.taxonomy import THRESHOLDS`, and add `call_paths` and
`run_key` to the harness import. Then append:

```python
@dataclass
class Run:
    """Consecutive calls in one turn that share a run key (a directory or an exec program)."""

    key: str
    lines: list[int]
    paths: list[str]
    referenced: bool = True


@dataclass
class TurnProfile:
    """Where one turn's effort went, relative to what its ask referenced."""

    ask_line: int
    ask_preview: str
    refs: list[str]
    calls: int
    runs: list[Run]


def focus_profile(session: ParsedSession) -> list[TurnProfile]:
    """Per user turn: the ask, its references, and the runs of calls it produced."""
    return [_profile(session, turn) for turn in user_turns(session)]


def _profile(session: ParsedSession, turn: TurnSpan) -> TurnProfile:
    runs: list[Run] = []
    for call in turn.calls:
        key = run_key(session.harness, call)
        paths = call_paths(session.harness, call)
        if runs and runs[-1].key == key:
            runs[-1].lines.append(call.line)
            runs[-1].paths.extend(paths)
        else:
            runs.append(Run(key, [call.line], list(paths)))
    _mark_referenced(runs, turn.ask.refs)
    return TurnProfile(turn.ask.line, turn.ask.preview, list(turn.ask.refs), len(turn.calls),
                       runs)


def _mark_referenced(runs: list[Run], refs: list[str]) -> None:
    """A run is referenced if it touches something the ask named, or a directory the turn
    already reached through something the ask named. A run with no paths stays referenced:
    there is no evidence either way."""
    if not refs:
        return
    reached: set[str] = set()
    for run in runs:
        if not run.paths:
            continue
        run.referenced = _program_named(run.key, refs) or any(
            _names(path, refs) or posixpath.dirname(path) in reached for path in run.paths)
        if run.referenced:
            reached.update(posixpath.dirname(p) for p in run.paths)


def _names(path: str, refs: list[str]) -> bool:
    base = posixpath.basename(path.rstrip("/"))
    for ref in refs:
        clean = ref.lstrip("@").rstrip("/")
        if "/" in clean and (clean in path or path in clean):
            return True
        if base and base == posixpath.basename(clean):
            return True
        if "/" not in clean and "." not in clean and len(clean) >= 3 and clean in path:
            return True
    return False


def _program_named(key: str, refs: list[str]) -> bool:
    return key.startswith("exec:") and key[len("exec:"):] in refs


def drift_candidates(session: ParsedSession) -> list[Anomaly]:
    """Unreferenced runs that dominate a turn, plus the calls before each user interrupt."""
    out: list[Anomaly] = []
    for profile in focus_profile(session):
        if not profile.refs:
            continue
        for run in profile.runs:
            share = len(run.lines) / profile.calls
            if run.referenced or len(run.lines) < THRESHOLDS["drift_min_calls"] \
                    or share < THRESHOLDS["drift_min_share"]:
                continue
            out.append(Anomaly("drift-candidate",
                               f"{len(run.lines)} calls ({share:.0%} of the turn) on {run.key}, "
                               "which the ask never referenced",
                               len(run.lines), run.lines[:MAX_LINES]))
    out.extend(_interrupt_drift(session))
    return out


def _interrupt_drift(session: ParsedSession) -> list[Anomaly]:
    """The user stopping the agent and redirecting it is direct evidence the preceding work
    went off course — cite the calls just before the interrupt, refs or not."""
    size = int(THRESHOLDS["interrupt_drift_calls"])
    out: list[Anomaly] = []
    for interrupt in user_interrupt(session):
        point = interrupt.lines[0]
        ask_line = max((m.line for m in session.messages
                        if m.role == "user" and m.text_len > 0 and m.line < point
                        and not is_interrupt_marker(m.preview)), default=0)
        window = [c.line for c in session.tools if ask_line < c.line <= point][-size:]
        if window:
            out.append(Anomaly("drift-candidate",
                               f"the {len(window)} calls before the user interrupt at line "
                               f"{point}", len(window), window))
    return out
```

- [ ] **Step 5: Register it**

Append `alignment.drift_candidates,` to `DETECTORS`.

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sessionkit/detectors sessionkit/taxonomy.py tests/test_detectors_alignment.py
git commit -m "Add the focus profile and drift candidates, including user-interrupt drift"
```

---

### Task 13: Cross-session resource interference, and registry completeness

**Files:**
- Create: `sessionkit/detectors/interference.py`.
- Modify: `sessionkit/corpus.py`. Add `_add_cross_session_findings`, called from `load()`.
- Modify: `sessionkit/taxonomy.py` (`THRESHOLDS` and `FINDING_KINDS`).
- Test: `tests/test_detectors_interference.py` and `tests/test_detectors_registry.py` (both
  new), plus one new test in `tests/test_corpus_pi.py`'s `PiAsyncRunTest`.

**Interfaces:**
- Consumes:
  - `Loaded.project_key` and `Loaded.anomalies`;
  - `harness.role` and `features.parse_ts`;
  - linked async runs (Task 6).
- Produces:
  - `interference.resource_interference(sessions: list[tuple[str, ParsedSession]]) ->
    list[list[Anomaly]]`;
  - the finding kind `resource-interference` (S->J).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detectors_interference.py`:

```python
"""Model errors that coincide with another session's long-running command."""

from __future__ import annotations

import unittest

from sessionkit.detectors.interference import resource_interference
from tests import detector_fixtures as df


def _parent(error_at: float):
    return df.session(sid="parent", messages=[
        df.msg(1, "user", text="go", at=df.ts(0)),
        df.msg(79, stop="error", error_text="Request timed out.", at=df.ts(error_at))])


def _child(start: float, minutes: float):
    return df.session(sid="child", tools=[df.call(
        55, "bash", {"command": "bash bench.sh"}, at=df.ts(start),
        result_at=df.ts(start + minutes), dur_ms=int(minutes * 60_000))])


class InterferenceTest(unittest.TestCase):
    def test_error_inside_a_sibling_command_window(self) -> None:
        found = resource_interference([("proj", _parent(65)), ("proj", _child(60, 41))])
        self.assertEqual([a.lines for a in found[0]], [[79]])
        self.assertEqual(found[1], [])

    def test_other_projects_do_not_interfere(self) -> None:
        found = resource_interference([("proj", _parent(65)), ("other", _child(60, 41))])
        self.assertEqual(found[0], [])

    def test_short_commands_are_ignored(self) -> None:
        found = resource_interference([("proj", _parent(60.5)), ("proj", _child(60, 1))])
        self.assertEqual(found[0], [])

    def test_errors_without_timestamps_are_skipped(self) -> None:
        parent = df.session(sid="parent", messages=[df.msg(79, stop="error",
                                                           error_text="Request timed out.")])
        self.assertEqual(resource_interference([("proj", parent), ("proj", _child(60, 41))])[0],
                         [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

Create `tests/test_detectors_registry.py`:

```python
"""The detector registry and the taxonomy agree, and empty sessions are handled."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from sessionkit import detectors, taxonomy
from sessionkit.classify import derive_end_state
from sessionkit.detectors import detect
from tests import detector_fixtures as df

_SOURCE = "".join(p.read_text(encoding="utf-8")
                  for p in Path(detectors.__file__).parent.glob("*.py"))
_LITERAL_KINDS = set(re.findall(r'Anomaly\(\s*"([a-z-]+)"', _SOURCE))


class RegistryTest(unittest.TestCase):
    def test_empty_sessions(self) -> None:
        for harness in ("pi", "claude-code", ""):
            with self.subTest(harness=harness):
                session = df.session(harness)
                self.assertEqual(detect(session), [])
                self.assertEqual(derive_end_state(session)[0], "unknown")

    def test_every_emitted_kind_is_in_the_taxonomy(self) -> None:
        self.assertTrue(_LITERAL_KINDS)
        self.assertEqual({k for k in _LITERAL_KINDS if taxonomy.lookup(k) is None}, set())

    def test_every_structural_or_candidate_kind_has_a_detector(self) -> None:
        kinds = {c.name for c in taxonomy.FINDING_KINDS if c.tier != taxonomy.TIER_J}
        self.assertEqual(kinds - _LITERAL_KINDS, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

Append to `PiAsyncRunTest` in `tests/test_corpus_pi.py`:

```python
    def test_parent_error_during_a_child_command_is_interference(self) -> None:
        fx.write(self.pi_home, [
            fx.header(), fx.user_msg("e1", None, "implement task 1"),
            fx.assistant_msg("e2", "e1", [], stop_reason="error",
                             error_message="Request timed out.", ts="2026-09-01T00:20:00.000Z"),
        ])
        child = [fx.header(sid=CHILD_SID),
                 fx.session_info("i1", None, f"subagent-developer-{RUN_ID}-1"),
                 fx.user_msg("e1", "i1", "Task 1"),
                 fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                     "b1", "bash", {"command": "bash bench.sh"})], stop_reason="toolUse",
                     ts="2026-09-01T00:10:00.000Z"),
                 fx.tool_result_msg("e3", "e2", "b1", "bash", "ok",
                                    ts="2026-09-01T00:40:00.000Z"),
                 fx.assistant_msg("e4", "e3", [{"type": "text", "text": "finished"}],
                                  ts="2026-09-01T00:41:00.000Z")]
        fx.write_async_run(self.pi_home, fx.PARENT_FILE, FOLDER, child)
        parent = self._load()[1][fx.SID]
        self.assertIn("resource-interference", [a.kind for a in parent.anomalies])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_detectors_interference tests.test_detectors_registry tests.test_corpus_pi -v`
Expected: FAIL. `sessionkit.detectors.interference` does not exist, and the corpus test finds
no `resource-interference` finding. The registry tests already pass at this point; they guard
the kind that Step 3 adds.

- [ ] **Step 3: Threshold and finding kind**

Add to `THRESHOLDS`:

```python
    "interference_exec_ms": 120_000,  # a concurrent command at least this long can interfere
```

Append to `FINDING_KINDS`:

```python
    _kind("resource-interference", "environment", TIER_CANDIDATE, "harness",
          "Model errors coincided with another session's long-running command in the same "
          "project — check whether that command used the resource the failing session depends "
          "on (for example, the model server)."),
```

- [ ] **Step 4: Create `sessionkit/detectors/interference.py`**

```python
"""Cross-session detector: a session's model errors while another session's long command ran.

This is the only detector that needs more than one session, so it runs as a corpus-level pass
after every session is loaded rather than inside :func:`sessionkit.detectors.detect`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sessionkit.detectors.base import Anomaly
from sessionkit.features import parse_ts
from sessionkit.harness import role
from sessionkit.parse import Message, ParsedSession
from sessionkit.taxonomy import THRESHOLDS


@dataclass
class _Window:
    sid: str
    name: str
    line: int
    start: datetime
    end: datetime


def _long_exec_windows(session: ParsedSession) -> list[_Window]:
    windows = []
    for call in session.tools:
        if role(session.harness, call.name) != "exec" or \
                (call.dur_ms or 0) < THRESHOLDS["interference_exec_ms"]:
            continue
        start, end = parse_ts(call.ts), parse_ts(call.result_ts)
        if start is not None and end is not None:
            windows.append(_Window(session.sid, call.name, call.line, start, end))
    return windows


def resource_interference(sessions: list[tuple[str, ParsedSession]]) -> list[list[Anomaly]]:
    """For each ``(project_key, session)``, the failed model calls that happened while another
    session in the same project was running a long command.

    Returns:
        One list of findings per input pair, in input order.
    """
    by_project: dict[str, list[int]] = {}
    for index, (project, _session) in enumerate(sessions):
        by_project.setdefault(project, []).append(index)
    found: list[list[Anomaly]] = [[] for _ in sessions]
    for index, (project, session) in enumerate(sessions):
        errors = [m for m in session.messages if m.stop_reason == "error"]
        if not errors:
            continue
        windows = [w for other in by_project[project] if other != index
                   for w in _long_exec_windows(sessions[other][1])]
        found[index] = _overlaps(errors, windows)
    return found


def _overlaps(errors: list[Message], windows: list[_Window]) -> list[Anomaly]:
    out: list[Anomaly] = []
    for message in errors:
        at = parse_ts(message.ts)
        hit = next((w for w in windows if at is not None and w.start <= at <= w.end), None)
        if hit is None:
            continue
        seconds = int((hit.end - hit.start).total_seconds())
        out.append(Anomaly("resource-interference",
                           f"model call failed while session {hit.sid[:8]} ran {hit.name} "
                           f"(line {hit.line}, {seconds}s)", 1, [message.line]))
    return out
```

- [ ] **Step 5: Run it after loading, in `sessionkit/corpus.py`**

Add the import `from sessionkit.detectors.interference import resource_interference`. Add this
function after `_link_async_runs`:

```python
def _add_cross_session_findings(sessions: list[Loaded]) -> None:
    """Findings that need several sessions at once (spec §2.2 ``resource-interference``)."""
    pairs = [(entry.project_key, entry.session) for entry in sessions]
    for entry, extra in zip(sessions, resource_interference(pairs)):
        entry.anomalies.extend(extra)
```

In `load()`, add `_add_cross_session_findings(corpus.sessions)` after
`_link_async_runs(corpus.sessions)`.

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sessionkit/detectors/interference.py sessionkit/corpus.py sessionkit/taxonomy.py \
  tests/test_detectors_interference.py tests/test_detectors_registry.py tests/test_corpus_pi.py
git commit -m "Flag model errors that overlap a sibling session's long-running command"
```
