# Checkpoint 1: Foundation refactor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this checkpoint task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Part of:** the agent post-mortem plan. Tracker: `2026-09-27-agent-postmortem-00-tracker.md`. Read its **Global Constraints**, **Spec deviations** and **Review Focus** before starting; they apply to every task here.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**Goal:** `taxonomy.py`, `harness.py` and the `detectors/` package replace the monolithic `classify.py`, with no behavior change.

**Starts from:** The branch as it is, with the full suite passing.

**Done when:** every task below is committed; `PYTHONPATH=. python3 -m unittest discover -s tests -t .` passes; and the tracker marks each task done with its commit.

---

### Task 1: `taxonomy.py`, the single failure table (refactor, no behavior change)

**Files:**
- Create: `sessionkit/taxonomy.py`
- Modify: `sessionkit/classify.py` (remove `THRESHOLDS`, `_RULES`, `_EXIT_CODES`, `_EXIT_RE`,
  `_NORMALISE`, `ANOMALY_HINTS`, `classify_error` and `signature`, and import them from
  taxonomy instead)
- Modify: `sessionkit/query.py:23` and `sessionkit/query.py:733-737` (`forensics_findings`)
- Modify: `sessionkit/cli.py:17`, and the docstring at `sessionkit/cli.py:246`
- Modify: `tests/test_classify.py` (import lines, plus `AnomalyHintTest`)
- Test: `tests/test_taxonomy.py` (new)

**Interfaces:**
- Produces:
  - `taxonomy.FailureClass(name, family, tier, fix_target, hint, patterns)`
  - `TIER_S`, `TIER_CANDIDATE`, `TIER_J`, and `THRESHOLDS: dict[str, float]`
  - `ERROR_CLASSES`, `KILLED`, `FINDING_KINDS` and `ALL_CLASSES`
  - `lookup(name) -> FailureClass | None`
  - `classify_error(text) -> tuple[str, str]` and `signature(text, limit=80) -> str`
- Families: `generic`, `tool-protocol`, `generation`, `loop`, `lifecycle`, `recovery`,
  `alignment`, `environment`.
- Fix targets: `model`, `harness`, `serving`, `env`, `none`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_taxonomy.py`:

```python
"""Tests for the failure taxonomy table itself: names, tiers, families, hints."""

from __future__ import annotations

import unittest

from sessionkit import taxonomy

VALID_FAMILIES = {"generic", "tool-protocol", "generation", "loop", "lifecycle", "recovery",
                  "alignment", "environment"}
VALID_TIERS = {taxonomy.TIER_S, taxonomy.TIER_CANDIDATE, taxonomy.TIER_J}
VALID_TARGETS = {"model", "harness", "serving", "env", "none"}


class TaxonomyTableTest(unittest.TestCase):
    """The table is well formed: unique names, valid enums, a hint everywhere."""

    def test_names_are_unique_across_classes_and_kinds(self) -> None:
        names = [c.name for c in taxonomy.ALL_CLASSES]
        self.assertEqual(len(names), len(set(names)))

    def test_every_class_is_well_formed(self) -> None:
        for entry in taxonomy.ALL_CLASSES:
            with self.subTest(entry.name):
                self.assertIn(entry.family, VALID_FAMILIES)
                self.assertIn(entry.tier, VALID_TIERS)
                self.assertIn(entry.fix_target, VALID_TARGETS)
                self.assertTrue(entry.hint)

    def test_error_classes_carry_patterns(self) -> None:
        for entry in taxonomy.ERROR_CLASSES:
            with self.subTest(entry.name):
                self.assertTrue(entry.patterns)

    def test_lookup(self) -> None:
        self.assertEqual(taxonomy.lookup("repeat-tool").family, "loop")
        self.assertIsNone(taxonomy.lookup("no-such-kind"))

    def test_killed_is_registered_for_the_exit_code_mapping(self) -> None:
        self.assertEqual(taxonomy.classify_error("Exit code 137")[0], "killed")
        self.assertIsNotNone(taxonomy.lookup("killed"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_taxonomy -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sessionkit.taxonomy'`

- [ ] **Step 3: Create `sessionkit/taxonomy.py`**

```python
"""The failure taxonomy: every error class and finding kind, in one table.

Each entry names a failure, says how it is detected (its tier), where its fix belongs (its fix
target), and gives a one-line hint. Error classes also carry the text patterns that recognise
them in a tool result; their order matters because patterns overlap — a hook block also
contains "exit".

The error-class list is grounded in the live corpus rather than guessed: clustering the 208
error results across 86 transcripts put ``hook-block`` first at 47 occurrences (23% of all
failures). Text that matches nothing lands in ``other`` with its normalised prefix retained,
so the taxonomy grows from real misses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TIER_S = "S"
TIER_CANDIDATE = "S->J"
TIER_J = "J"

#: All tuneable detector limits in one place rather than scattered as literals.
THRESHOLDS: dict[str, float] = {
    "repeat_tool": 3,       # identical (tool, input) calls in one session
    "file_thrash": 5,       # edits to the same path
    "read_loop": 4,         # reads of the same path
    "error_cascade": 3,     # consecutive failing tool calls
    "hook_pingpong": 2,     # hook blocks against the same tool
    "compaction_churn": 2,  # compaction events
    "stall_ms": 120_000,    # a single tool call taking longer than this
}


@dataclass(frozen=True)
class FailureClass:
    """One named failure: an error class matched in result text, or a finding kind.

    ``fix_target`` is where the remedy belongs: ``model``, ``harness`` (instructions file, tool
    config, token limits), ``serving`` (vLLM parser, chat template, sampling), ``env``, or
    ``none``.
    """

    name: str
    family: str
    tier: str
    fix_target: str
    hint: str
    patterns: tuple[re.Pattern[str], ...] = ()


def _error(name: str, family: str, fix_target: str, hint: str, *regexes: str,
           flags: int = re.I) -> FailureClass:
    """An error class: structural tier, recognised by its patterns."""
    patterns = tuple(re.compile(r, flags) for r in regexes)
    return FailureClass(name, family, TIER_S, fix_target, hint, patterns)


def _kind(name: str, family: str, tier: str, fix_target: str, hint: str) -> FailureClass:
    """A finding kind: emitted by a detector, or (tier J) judged by the skill."""
    return FailureClass(name, family, tier, fix_target, hint)


#: Ordered: the first class whose pattern matches wins, so specific classes precede generic
#: ones (a denied mkdir arrives as "Exit code 1 ... Permission denied").
ERROR_CLASSES: tuple[FailureClass, ...] = (
    _error("hook-block", "environment", "harness",
           "Promote the rule into CLAUDE.md so the command is never emitted.",
           r"PreToolUse:|PostToolUse:.*hook error|\bBLOCKED:"),
    _error("user-rejected", "environment", "none",
           "Approach problem — the model proposed something the user declined.",
           r"user doesn't want to proceed|tool use was rejected"),
    # Distinct from user-rejected: no human saw this one, the harness blocked it.
    _error("policy-denied", "environment", "harness",
           "Blocked by permission policy — needs approval or a different approach.",
           r"denied by the Claude Code|auto mode classifier|"
           r"permission for this action was denied|"
           r"permission to use \w+.{0,200}?has been denied", flags=re.I | re.S),
    # A malformed call is the model's own bug, not the environment's.
    _error("input-validation", "tool-protocol", "model",
           "Malformed tool call — the model passed a wrong argument type.",
           r"InputValidationError|parameter `\w+` type is expected"),
    _error("file-too-large", "tool-protocol", "model",
           "Read with offset/limit, or use Grep instead of a full read.",
           r"exceeds maximum allowed tokens"),
    _error("read-truncated", "tool-protocol", "model",
           "Narrow the read; the model only saw part of the file.",
           r"read_truncation|truncated to \d+ lines"),
    _error("stale-read", "tool-protocol", "model",
           "Re-read the file before editing; another writer changed it.",
           r"has been modified since read|file has changed since"),
    _error("unread-file", "tool-protocol", "model",
           "Read before Write/Edit — the tool requires it.",
           r"has not been read yet|read it first"),
    _error("missing-tool", "environment", "env",
           "Install the tool or drop the dependency on it.",
           r"command not found|: not found$|\bno such command\b"),
    # Must precede exit-code: a denied mkdir surfaces as "Exit code 1 ... Permission denied".
    _error("permission-denied", "environment", "env",
           "Write target is not writable by this user — pick a writable path.",
           r"EACCES|permission denied|EPERM\b"),
    _error("not-a-repo", "generic", "none",
           "cwd assumption — the command ran outside a repo.",
           r"not a git repository"),
    _error("not-found", "generic", "none",
           "Path assumption — verify the path before operating on it.",
           r"File does not exist|Directory does not exist|no such file or directory|"
           r"\bENOENT\b"),
    _error("rate-limit", "environment", "env",
           "Back off, or route to an alternate provider.",
           r"rate limit|429\b|too many requests"),
    _error("api-error", "environment", "env",
           "Transient or misconfigured endpoint — check which.",
           r"request failed with status code|\b5\d\d\b error"),
    _error("mcp-error", "environment", "env",
           "MCP server health — check the server, not the prompt.",
           r"error in mcp__|mcp server .* (failed|error)"),
    _error("timeout", "generic", "none",
           "Raise the timeout or split the work.",
           r"timed out|timeout exceeded"),
    _error("exit-code", "generic", "none",
           "Genuine command failure — read the command output.",
           r"^exit code \d+"),
)

#: Exit codes whose meaning is unambiguous. Checked *before* the body-text rules because the
#: code is the tool's own verdict about the call, while body text is often incidental — a
#: compound command's stray ``ls: No such file`` should not outrank the fact that the call
#: itself timed out.
_EXIT_CODES: dict[int, tuple[str, str]] = {
    124: ("timeout", "Command hit its timeout — raise it or split the work."),
    137: ("killed", "Killed (SIGKILL, usually OOM) — the work did not complete."),
    143: ("killed", "Terminated (SIGTERM) — the work did not complete."),
}
_EXIT_RE = re.compile(r"^exit code (\d+)\b", re.I)

#: Reached only through ``_EXIT_CODES``, so it has no text patterns of its own.
KILLED = _kind("killed", "generic", TIER_S, "env",
               "Killed (SIGKILL, usually OOM) or terminated — the work did not complete.")

FINDING_KINDS: tuple[FailureClass, ...] = (
    _kind("repeat-tool", "loop", TIER_S, "model",
          "Same call retried with identical input — check why the prior result wasn't used "
          "instead of re-running it."),
    _kind("file-thrash", "loop", TIER_S, "model",
          "Same file edited repeatedly — plan the full change before editing, or use "
          "MultiEdit for a single pass."),
    _kind("read-loop", "loop", TIER_S, "model",
          "Same file re-read repeatedly — remember its contents instead of re-reading."),
    _kind("error-cascade", "loop", TIER_S, "model",
          "Consecutive failures — stop and diagnose the root cause instead of retrying "
          "variants of the same call."),
    _kind("hook-pingpong", "loop", TIER_S, "harness",
          "The same tool was blocked by a hook repeatedly — the rule belongs in CLAUDE.md, "
          "where it teaches at generation time instead of blocking at execution time."),
    _kind("agent-kill", "lifecycle", TIER_S, "none",
          "A subagent was killed before returning — check the dispatch conditions or the "
          "timeout that ended it."),
    _kind("orphan-subagent", "lifecycle", TIER_S, "harness",
          "A subagent was spawned and never returned — check whether it stalled or the "
          "parent moved on without waiting."),
    _kind("compaction-churn", "lifecycle", TIER_S, "harness",
          "Context was compacted more than once — the session outgrew its window; consider "
          "splitting the work or narrowing scope."),
    _kind("stall", "lifecycle", TIER_S, "env",
          "A single tool call ran unusually long — check for a network hang or an unbounded "
          "operation."),
    _kind("rejection-persist", "loop", TIER_S, "model",
          "A call was retried after the user explicitly rejected it — respect the rejection "
          "and ask before retrying, rather than trying again unprompted."),
)

ALL_CLASSES: tuple[FailureClass, ...] = (*ERROR_CLASSES, KILLED, *FINDING_KINDS)
_REGISTRY: dict[str, FailureClass] = {c.name: c for c in ALL_CLASSES}

_NORMALISE = [
    (re.compile(r"/[\w./\-]{6,}"), "<PATH>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def lookup(name: str) -> FailureClass | None:
    """The taxonomy entry for an error class or finding kind, or ``None``."""
    return _REGISTRY.get(name)


def classify_error(text: str) -> tuple[str, str]:
    """Classify an error result body.

    Args:
        text: The tool result text (already redacted and previewed).

    Returns:
        ``(class, fix_hint)``. Unmatched text returns ``("other", "")``.
    """
    body = (text or "").strip()
    if not body:
        return ("empty", "")
    match = _EXIT_RE.match(body)
    if match:
        known = _EXIT_CODES.get(int(match.group(1)))
        if known:
            return known
    for entry in ERROR_CLASSES:
        if any(p.search(body) for p in entry.patterns):
            return (entry.name, entry.hint)
    return ("other", "")


def signature(text: str, limit: int = 80) -> str:
    """Collapse an error body into a clusterable signature.

    Digits, hex and long paths are the parts that differ between otherwise-identical failures,
    so they are replaced with placeholders before truncation.
    """
    body = (text or "").strip()
    for pattern, replacement in _NORMALISE:
        body = pattern.sub(replacement, body)
    return body[:limit]
```

- [ ] **Step 4: Point `classify.py` at the taxonomy**

In `sessionkit/classify.py`:
- **Delete** these blocks: the module-level `THRESHOLDS` dict, `_RULES`, `_EXIT_CODES`,
  `_EXIT_RE`, `_NORMALISE`, `ANOMALY_HINTS`, `def classify_error` and `def signature`.
- **Replace** the module docstring with:

```python
"""Session end-state derivation, tail signals, and anomaly detection.

The failure taxonomy (error classes, hints, thresholds) lives in ``sessionkit.taxonomy``.
"""
```

- **Replace** the import block with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from sessionkit.parse import ParsedSession, SysEvent, ToolCall
from sessionkit.taxonomy import THRESHOLDS, classify_error, signature
```

- [ ] **Step 5: Update the consumers**

`sessionkit/query.py` line 23 becomes:

```python
from sessionkit.taxonomy import lookup, signature
```

Replace `forensics_findings` in `sessionkit/query.py` with:

```python
def forensics_findings(entry: Loaded) -> list[Row]:
    """One session's anomalies, most frequent first, each with its prevention hint."""
    ordered = sorted(entry.anomalies, key=lambda a: -a.count)
    rows = []
    for a in ordered:
        known = lookup(a.kind)
        rows.append({"kind": a.kind, "detail": a.detail, "count": a.count, "lines": a.lines,
                     "hint": known.hint if known else ""})
    return rows
```

`sessionkit/cli.py` line 17 becomes:

```python
from sessionkit.taxonomy import classify_error
```

In the `cmd_forensics` docstring, replace ``classify.ANOMALY_HINTS`` with ``taxonomy``.

- [ ] **Step 6: Update `tests/test_classify.py`**

Replace the import block (lines 11–13) with:

```python
from sessionkit.classify import annotate_errors, derive_end_state, detect
from sessionkit.parse import FileOp, ParsedSession, SysEvent, ToolCall
from sessionkit.taxonomy import classify_error, signature
```

Replace `AnomalyHintTest.test_every_detector_kind_has_a_hint` with:

```python
    def test_every_detector_kind_has_a_hint(self) -> None:
        from sessionkit.taxonomy import lookup
        known_kinds = {"repeat-tool", "file-thrash", "read-loop", "error-cascade",
                       "hook-pingpong", "agent-kill", "orphan-subagent", "compaction-churn",
                       "stall", "rejection-persist"}
        for kind in known_kinds:
            with self.subTest(kind):
                entry = lookup(kind)
                self.assertIsNotNone(entry)
                self.assertTrue(entry.hint)
```

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS. That's 297 existing tests plus 5 new ones, with no assertion changes other
than the rewritten hint test.

- [ ] **Step 8: Commit**

```bash
git add sessionkit/taxonomy.py sessionkit/classify.py sessionkit/query.py sessionkit/cli.py \
  tests/test_taxonomy.py tests/test_classify.py
git commit -m "Move the failure taxonomy into one table in taxonomy.py"
```

---

### Task 2: `harness.py` tool roles and the `detectors/` package (refactor, no behavior change)

**Files:**
- Create: `sessionkit/harness.py`
- Create: `sessionkit/detectors/__init__.py`, `sessionkit/detectors/base.py`,
  `sessionkit/detectors/loops.py`, `sessionkit/detectors/lifecycle.py`
- Modify: `sessionkit/parse.py` (`ParsedSession` gains `harness: str = ""`)
- Modify: `sessionkit/classify.py`: delete `Anomaly`, the detector functions, `_DETECTORS`
  and `detect`; rename `_trailing_errors` to `trailing_errors`
- Modify: `sessionkit/corpus.py:25` (imports)
- Modify: `sessionkit/query.py` (`children_rows` uses role `dispatch`)
- Modify: `tests/test_classify.py` (the `detect` import)
- Test: `tests/test_harness.py` (new)

**Interfaces:**
- Consumes: `taxonomy.THRESHOLDS` (Task 1).
- Produces:
  - `harness.ROLES`
  - `role(harness, tool_name) -> str`
  - `call_args(call) -> dict`
  - `command_of(call) -> str`
  - `call_paths(harness, call) -> list[str]`
  - `command_program(command) -> str`
  - `run_key(harness, call) -> str`
  - `declared_timeout_ms(harness, call) -> int | None`
  - `is_verify_command(command) -> bool`
  - `is_interrupt_marker(text) -> bool`
  - `detectors.Anomaly(kind, detail, count, lines)`
  - `detectors.detect(session) -> list[Anomaly]`
  - `detectors.base.MAX_LINES = 8`
  - `classify.trailing_errors(tools) -> int`
  - `ParsedSession.harness`

- [ ] **Step 1: Write the failing test**

Create `tests/test_harness.py`:

```python
"""Tests for per-harness tool roles and argument extraction."""

from __future__ import annotations

import json
import unittest

from sessionkit import harness
from sessionkit.parse import ToolCall


def _call(name: str, args: object = None, raw: str | None = None) -> ToolCall:
    preview = raw if raw is not None else json.dumps(args if args is not None else {})
    return ToolCall(tool_use_id="t", line=1, name=name, ts="", input_preview=preview)


class RoleTest(unittest.TestCase):
    def test_claude_code_roles(self) -> None:
        self.assertEqual(harness.role("claude-code", "Read"), "read")
        self.assertEqual(harness.role("claude-code", "Bash"), "exec")
        self.assertEqual(harness.role("claude-code", "Agent"), "dispatch")
        self.assertEqual(harness.role("claude-code", "AskUserQuestion"), "ask-user")

    def test_pi_roles(self) -> None:
        self.assertEqual(harness.role("pi", "read"), "read")
        self.assertEqual(harness.role("pi", "grep"), "search")
        self.assertEqual(harness.role("pi", "bash"), "exec")
        self.assertEqual(harness.role("pi", "subagent"), "dispatch")
        self.assertEqual(harness.role("pi", "bg_wait"), "poll")

    def test_unknown_harness_falls_back_to_every_table(self) -> None:
        self.assertEqual(harness.role("", "Edit"), "edit")
        self.assertEqual(harness.role("", "bash"), "exec")

    def test_unknown_tool_is_other(self) -> None:
        self.assertEqual(harness.role("pi", "mcp__server__tool"), "other")


class PathTest(unittest.TestCase):
    def test_path_from_json_args(self) -> None:
        call = _call("Read", {"file_path": "/a/b.py"})
        self.assertEqual(harness.call_paths("claude-code", call), ["/a/b.py"])

    def test_path_from_truncated_preview(self) -> None:
        raw = ('{"path": "/a/b.py", "content": "' + "x" * 3000)[:2000]
        self.assertEqual(harness.call_paths("pi", _call("write", raw=raw)), ["/a/b.py"])

    def test_exec_paths_from_command(self) -> None:
        call = _call("bash", {"command": "cd /repo && cat docs/plan.md | head"})
        self.assertEqual(harness.call_paths("pi", call), ["/repo", "docs/plan.md"])

    def test_exec_command_recovered_from_truncated_preview(self) -> None:
        call = _call("bash", raw='{"command": "rg -n foo /src/app.py')
        self.assertEqual(harness.command_of(call), "rg -n foo /src/app.py")
        self.assertEqual(harness.call_paths("pi", call), ["/src/app.py"])

    def test_raw_bash_execution_command(self) -> None:
        self.assertEqual(harness.command_of(_call("bash", raw="ls -la /tmp")), "ls -la /tmp")

    def test_urls_and_flags_are_not_paths(self) -> None:
        call = _call("bash", {"command": "curl -s https://x.dev/a/b -o out.json"})
        self.assertEqual(harness.call_paths("pi", call), ["out.json"])


class ProgramTest(unittest.TestCase):
    def test_skips_cd_env_assignments_and_wrappers(self) -> None:
        self.assertEqual(harness.command_program("cd /repo && git status"), "git")
        self.assertEqual(harness.command_program("PKG=$(npm root -g)/x; rg -l foo $PKG"), "rg")
        self.assertEqual(harness.command_program("sudo docker run x"), "docker")
        self.assertEqual(harness.command_program("/usr/local/bin/llama-swap --help"),
                         "llama-swap")

    def test_run_key(self) -> None:
        self.assertEqual(harness.run_key("pi", _call("bash", {"command": "rg x"})), "exec:rg")
        self.assertEqual(harness.run_key("claude-code", _call("Read", {"file_path": "/a/b.py"})),
                         "dir:/a")
        self.assertEqual(harness.run_key("claude-code", _call("Skill", {})), "other:Skill")


class TimeoutTest(unittest.TestCase):
    def test_pi_timeout_is_seconds(self) -> None:
        call = _call("bash", {"command": "x", "timeout": 30})
        self.assertEqual(harness.declared_timeout_ms("pi", call), 30_000)

    def test_claude_code_timeout_is_milliseconds(self) -> None:
        call = _call("Bash", {"command": "x", "timeout": 600_000})
        self.assertEqual(harness.declared_timeout_ms("claude-code", call), 600_000)

    def test_absent_or_invalid_timeout(self) -> None:
        self.assertIsNone(harness.declared_timeout_ms("pi", _call("bash", {"command": "x"})))
        self.assertIsNone(harness.declared_timeout_ms("pi", _call("bash", {"timeout": True})))
        self.assertIsNone(harness.declared_timeout_ms("pi", _call("bash", {"timeout": -1})))


class VerifyTest(unittest.TestCase):
    def test_verification_commands(self) -> None:
        for command in ("PYTHONPATH=. python3 -m unittest discover", "pytest -q",
                        "cargo test", "npm run test", "make check", "bash -n x.sh"):
            with self.subTest(command):
                self.assertTrue(harness.is_verify_command(command))

    def test_non_verification_commands(self) -> None:
        for command in ("ls -la", "cat Makefile", "git status"):
            with self.subTest(command):
                self.assertFalse(harness.is_verify_command(command))


class InterruptMarkerTest(unittest.TestCase):
    def test_claude_code_interrupt_marker(self) -> None:
        self.assertTrue(harness.is_interrupt_marker("[Request interrupted by user for tool use]"))
        self.assertFalse(harness.is_interrupt_marker("please interrupt the build"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_harness -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sessionkit.harness'`

- [ ] **Step 3: Create `sessionkit/harness.py`**

```python
"""Per-harness knowledge: which role each tool plays, and where its arguments keep paths.

Detectors key on roles (``read``, ``exec``, ``dispatch`` ...) instead of tool names, so a new
harness adds rows here instead of branches in every detector. Claude Code capitalises its tool
names and pi lowercases them, so the two tables never collide and a session with no recorded
harness can fall back to their union.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from sessionkit.parse import ToolCall

ROLES = ("read", "search", "edit", "write", "exec", "dispatch", "poll", "ask-user", "other")

_TOOL_ROLES: dict[str, dict[str, str]] = {
    "claude-code": {
        "Read": "read", "NotebookRead": "read",
        "Grep": "search", "Glob": "search", "LS": "search", "WebSearch": "search",
        "WebFetch": "search", "ToolSearch": "search",
        "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
        "Write": "write",
        "Bash": "exec",
        "Agent": "dispatch", "Task": "dispatch",
        "TaskOutput": "poll", "BashOutput": "poll", "TaskGet": "poll", "TaskList": "poll",
        "ListAgents": "poll",
        "AskUserQuestion": "ask-user", "ExitPlanMode": "ask-user",
    },
    "pi": {
        "read": "read",
        "grep": "search", "find": "search", "ls": "search",
        "edit": "edit",
        "write": "write",
        "bash": "exec",
        "subagent": "dispatch",
        "bg_wait": "poll",
    },
}
_ANY_HARNESS: dict[str, str] = {
    name: tool_role for table in _TOOL_ROLES.values() for name, tool_role in table.items()
}

#: Harnesses whose ``timeout`` argument is in seconds rather than milliseconds.
_TIMEOUT_SECONDS = frozenset({"pi"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
_PATH_RE = re.compile(r'"(?:file_path|notebook_path|path)"\s*:\s*"([^"]+)"')
_COMMAND_RE = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)')
_TOKEN_SPLIT = re.compile(r"[\s;|&<>()'\"]+")
_PATHLIKE = re.compile(
    r"^(?:~|\.{1,2})?/[\w.@-]+(?:/[\w.@-]*)*$"   # absolute or ~/ ./ ../
    r"|^[\w.@-]+(?:/[\w.@-]*)+$"                  # relative with a slash
    r"|^[\w-]+\.[A-Za-z]{1,5}$")                  # bare filename with an extension
_SUBSTITUTION = re.compile(r"\$\([^)]*\)|`[^`]*`")
_ENV_ASSIGN = re.compile(r"^\w+=")
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec"})
_INTERRUPT_MARKERS = ("[Request interrupted by user",)

VERIFY_COMMANDS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"\bpytest\b", r"\bunittest\b", r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
    r"\bcargo\s+(?:test|check|clippy)\b", r"\bgo\s+(?:test|vet)\b", r"\bmake\b",
    r"\btsc\b", r"\bruff\b", r"\bty\s+check\b", r"\bvitest\b", r"\bmypy\b",
    r"\bshellcheck\b", r"\bbash\s+-n\b",
))


def role(harness: str, tool_name: str) -> str:
    """The role a tool plays, falling back to every known harness's table, then ``other``."""
    return _TOOL_ROLES.get(harness, {}).get(tool_name) or _ANY_HARNESS.get(tool_name, "other")


def call_args(call: ToolCall) -> dict[str, Any]:
    """The call's input as a dict; ``{}`` when the 2,000-char preview cut the JSON short."""
    try:
        parsed = json.loads(call.input_preview)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def command_of(call: ToolCall) -> str:
    """The shell command of an ``exec`` call, recovered even from a truncated preview."""
    value = call_args(call).get("command")
    if isinstance(value, str):
        return value
    match = _COMMAND_RE.search(call.input_preview)
    if match:
        return match.group(1)
    # pi's bashExecution records (user `!` commands) keep the raw command, not JSON.
    return "" if call.input_preview.startswith("{") else call.input_preview


def call_paths(harness: str, call: ToolCall) -> list[str]:
    """Paths a call touches: its path argument, or the path-like tokens of an exec command."""
    if role(harness, call.name) == "exec":
        return _command_paths(command_of(call))
    args = call_args(call)
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return [value]
    match = _PATH_RE.search(call.input_preview)
    return [match.group(1)] if match else []


def _command_paths(command: str) -> list[str]:
    paths = []
    for token in _TOKEN_SPLIT.split(command):
        if not token or token.startswith("-") or "://" in token:
            continue
        if _PATHLIKE.match(token):
            paths.append(token)
    return paths


def command_program(command: str) -> str:
    """The program a shell command runs, skipping ``cd x &&``, env assignments, ``$(...)``
    substitutions and wrappers such as ``sudo``."""
    cleaned = _SUBSTITUTION.sub("", command)
    for segment in re.split(r"&&|\|\||;|\|", cleaned):
        words = segment.split()
        while words and (_ENV_ASSIGN.match(words[0]) or words[0] in _WRAPPERS):
            words.pop(0)
        if words and words[0] != "cd":
            return posixpath.basename(words[0])
    return ""


def run_key(harness: str, call: ToolCall) -> str:
    """What groups consecutive calls into one run: a directory, or an exec program."""
    call_role = role(harness, call.name)
    if call_role == "exec":
        return f"exec:{command_program(command_of(call))}"
    paths = call_paths(harness, call)
    if paths:
        return f"dir:{posixpath.dirname(paths[0].rstrip('/')) or paths[0]}"
    return f"{call_role}:{call.name}"


def declared_timeout_ms(harness: str, call: ToolCall) -> int | None:
    """The timeout the call itself declared, in milliseconds, or ``None``."""
    value = call_args(call).get("timeout")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value * 1000) if harness in _TIMEOUT_SECONDS else int(value)


def is_verify_command(command: str) -> bool:
    """Whether a shell command runs tests, a build, a type check or a linter."""
    return any(p.search(command) for p in VERIFY_COMMANDS)


def is_interrupt_marker(text: str) -> bool:
    """Whether a user message is Claude Code's synthetic "interrupted by user" marker."""
    return text.startswith(_INTERRUPT_MARKERS)
```

- [ ] **Step 4: Add `ParsedSession.harness`**

In `sessionkit/parse.py`, inside `ParsedSession`, add one line immediately after the existing
`path: str` field:

```python
    harness: str = ""
```

- [ ] **Step 5: Create the detectors package**

`sessionkit/detectors/base.py`:

```python
"""The shape every detector returns."""

from __future__ import annotations

from dataclasses import dataclass

#: Cap on the line numbers a finding cites, so one huge loop doesn't flood a report.
MAX_LINES = 8


@dataclass
class Anomaly:
    """One detected problem in a session."""

    kind: str
    detail: str
    count: int
    lines: list[int]
```

`sessionkit/detectors/loops.py`:

```python
"""Loop detectors: the same call, file or failure, over and over."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def repeat_tool(session: ParsedSession) -> list[Anomaly]:
    """Identical (tool, input) invoked repeatedly — the loop signal."""
    seen: dict[tuple[str, str], list[int]] = {}
    for call in session.tools:
        seen.setdefault((call.name, call.input_digest), []).append(call.line)
    out = []
    for (name, _digest), lines in seen.items():
        if len(lines) >= THRESHOLDS["repeat_tool"]:
            out.append(Anomaly("repeat-tool", f"{name} called with identical input",
                               len(lines), lines[:MAX_LINES]))
    return out


def _path_churn(session: ParsedSession, op: str, key: str, kind: str) -> list[Anomaly]:
    """Shared implementation for edit-thrash and read-loop detection."""
    lines: dict[str, list[int]] = {}
    for f in session.files:
        if f.op == op:
            lines.setdefault(f.path, []).append(f.line)
    limit = THRESHOLDS[key]
    return [Anomaly(kind, path, len(ls), ls) for path, ls in lines.items() if len(ls) >= limit]


def file_thrash(session: ParsedSession) -> list[Anomaly]:
    """The same path edited repeatedly."""
    return _path_churn(session, "edit", "file_thrash", "file-thrash")


def read_loop(session: ParsedSession) -> list[Anomaly]:
    """The same path read repeatedly."""
    return _path_churn(session, "read", "read_loop", "read-loop")


def error_cascade(session: ParsedSession) -> list[Anomaly]:
    """Runs of consecutive failing tool calls."""
    out: list[Anomaly] = []
    run, lines = 0, []
    for call in session.tools:
        if call.is_error:
            run += 1
            lines.append(call.line)
        else:
            if run >= THRESHOLDS["error_cascade"]:
                out.append(Anomaly("error-cascade", "consecutive failures", run,
                                   lines[:MAX_LINES]))
            run, lines = 0, []
    if run >= THRESHOLDS["error_cascade"]:
        out.append(Anomaly("error-cascade", "consecutive failures", run, lines[:MAX_LINES]))
    return out


def hook_pingpong(session: ParsedSession) -> list[Anomaly]:
    """Repeated hook blocks against the same tool — a self-inflicted retry loop."""
    counts: dict[str, list[int]] = {}
    for call in session.tools:
        if call.err_class == "hook-block":
            counts.setdefault(call.name, []).append(call.line)
    return [Anomaly("hook-pingpong", f"{name} blocked by a hook repeatedly", len(lines),
                    lines[:MAX_LINES])
            for name, lines in counts.items() if len(lines) >= THRESHOLDS["hook_pingpong"]]


def rejection_persist(session: ParsedSession) -> list[Anomaly]:
    """The same call retried after the user explicitly rejected it."""
    rejected: set[tuple[str, str]] = set()
    out = []
    for call in session.tools:
        key = (call.name, call.input_digest)
        if key in rejected and not call.is_error:
            out.append(Anomaly("rejection-persist", f"{call.name} retried after rejection",
                               1, [call.line]))
            rejected.discard(key)
        if call.err_class == "user-rejected":
            rejected.add(key)
    return out
```

`sessionkit/detectors/lifecycle.py`:

```python
"""Lifecycle detectors: how a session's calls and subagents ran, stalled or ended."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.harness import role
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def agent_trouble(session: ParsedSession) -> list[Anomaly]:
    """Killed or never-returning subagents."""
    out = []
    kills = [e.line for e in session.sysev if e.subtype == "agents_killed"]
    if kills:
        out.append(Anomaly("agent-kill", "subagent killed", len(kills), kills[:MAX_LINES]))
    orphans = [c.line for c in session.tools
               if role(session.harness, c.name) == "dispatch" and c.err_class == "no-result"]
    if orphans:
        out.append(Anomaly("orphan-subagent", "Agent spawned, never returned",
                           len(orphans), orphans[:MAX_LINES]))
    return out


def compaction_churn(session: ParsedSession) -> list[Anomaly]:
    """Repeated context compaction — the session outgrew its window more than once."""
    lines = [e.line for e in session.sysev if e.subtype == "compact_boundary"]
    if len(lines) >= THRESHOLDS["compaction_churn"]:
        return [Anomaly("compaction-churn", "context compacted repeatedly", len(lines),
                        lines[:MAX_LINES])]
    return []


def stalls(session: ParsedSession) -> list[Anomaly]:
    """Individual tool calls that ran far longer than the threshold."""
    limit = THRESHOLDS["stall_ms"]
    slow = [c for c in session.tools if c.dur_ms is not None and c.dur_ms > limit]
    return [Anomaly("stall", f"{c.name} took {c.dur_ms // 1000}s", 1, [c.line]) for c in slow]
```

`sessionkit/detectors/__init__.py`:

```python
"""Anomaly detectors: pure functions from a parsed session to its findings.

Each module groups one failure family (spec §2.2). A detector reads only the session plus the
taxonomy and harness tables — never a file — so the same functions work on a partial session.
"""

from __future__ import annotations

from typing import Callable

from sessionkit.detectors import lifecycle, loops
from sessionkit.detectors.base import Anomaly
from sessionkit.parse import ParsedSession

DETECTORS: list[Callable[[ParsedSession], list[Anomaly]]] = [
    loops.repeat_tool,
    loops.file_thrash,
    loops.read_loop,
    loops.error_cascade,
    loops.hook_pingpong,
    lifecycle.agent_trouble,
    lifecycle.compaction_churn,
    lifecycle.stalls,
    loops.rejection_persist,
]


def detect(session: ParsedSession) -> list[Anomaly]:
    """Run every detector against a session.

    Args:
        session: A session whose errors have already been annotated.

    Returns:
        Anomalies sorted by count, most frequent first.
    """
    found: list[Anomaly] = []
    for detector in DETECTORS:
        found.extend(detector(session))
    return sorted(found, key=lambda a: -a.count)


__all__ = ["Anomaly", "detect"]
```

- [ ] **Step 6: Strip the moved code from `classify.py`**

In `sessionkit/classify.py`:
- **Delete:** the `Anomaly` dataclass, every function from `def _repeat_tool` to the end of
  the file (`_repeat_tool`, `_path_churn`, `_error_cascade`, `_hook_pingpong`,
  `_agent_trouble`, `_compaction_churn`, `_stalls`, `_rejection_persist`, `_DETECTORS`,
  `detect`), and the now-unused `from dataclasses import dataclass` and
  `from typing import Callable` imports.
- **Rename:** `_trailing_errors` to `trailing_errors`, both at its definition and at its call
  in `derive_end_state`.
- **Replace** the module docstring with:

```python
"""Session end-state derivation and tail signals.

The failure taxonomy lives in ``sessionkit.taxonomy`` and the anomaly detectors in
``sessionkit.detectors``; this module decides how a session *ended*.
"""
```

- [ ] **Step 7: Update the consumers**

`sessionkit/corpus.py` line 25 becomes two lines:

```python
from sessionkit.classify import annotate_errors, derive_end_state
from sessionkit.detectors import Anomaly, detect
```

In `sessionkit/query.py`:
- Add `from sessionkit.harness import role` to the imports.
- In `children_rows`, replace `if tool.name not in ("Agent", "subagent"):` with:

```python
        if role(entry.session.harness, tool.name) != "dispatch":
```

In `tests/test_classify.py`, the import block becomes:

```python
from sessionkit.classify import annotate_errors, derive_end_state
from sessionkit.detectors import detect
from sessionkit.parse import FileOp, ParsedSession, SysEvent, ToolCall
from sessionkit.taxonomy import classify_error, signature
```

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS, with no assertion changes.

The only behavior widening is that Claude Code's legacy `Task` tool now also counts as a
dispatch. No existing test depends on `Task` not being one.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/harness.py sessionkit/detectors sessionkit/parse.py sessionkit/classify.py \
  sessionkit/corpus.py sessionkit/query.py tests/test_harness.py tests/test_classify.py
git commit -m "Split detectors into a package keyed on per-harness tool roles"
```
