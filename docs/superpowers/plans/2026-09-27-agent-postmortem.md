# Agent Post-Mortem Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sk` detect, and the `session-forensics` skill explain, why a Claude Code or pi
agent session went wrong: crashes, stalls and hangs, loops, drift, and tool-calling
failures. Each finding comes with a fix aimed at the model, the harness, the serving stack or
the environment.

**Architecture:** `classify.py` splits into four units:
- `taxonomy.py`: one table of every failure class, with its tier, fix target, hint and
  patterns.
- `harness.py`: tool names mapped to roles, per harness.
- `features.py`: compact features that parsers extract once from the full message text.
- `detectors/`: pure functions `(session) -> findings`, one module per failure family.

Parsers fill additive `Message`/`ParsedSession` fields, so detectors never re-read files. On
top of that:
- `sk forensics` becomes the post-mortem report.
- A new `sk findings` command aggregates findings across the fleet, with rates.
- The `session-forensics` skill gains a tiered procedure and a failure-class knowledge base.

**Tech Stack:** Python 3.11+ standard library only; `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

## Global Constraints

- **Language and dependencies:** system Python 3.11+ with zero third-party imports.
- **Imports:** absolute only (`from sessionkit.x import y`).
- **Size limits:** lines ≤ 100 characters, functions ≤ 100 lines.
- **Data model:** changes are **additive fields with defaults**. The one deliberate meaning
  change is that `Message.text_len` and `Message.preview` become *visible text only*, with
  reasoning moving to `Message.reasoning_len` (spec §4).
- **Tests:** `unittest`, built from inline fixture builders (`tests/fixtures.py`,
  `tests/fixtures_pi.py`, `tests/detector_fixtures.py`). Never add committed `.jsonl` files.
- **Test command:** run from the worktree root:
  `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
- **Existing tests:** when an existing test fails, it must be one this task names as an
  intended change. Any other failure is a regression to fix in the code, not in the test.
- **Thresholds:** every detector threshold lives in `taxonomy.THRESHOLDS`.
- **Out of scope:** no LLM calls inside `sk`, no live guard, no codex or opencode parser.
- **`test/` holds real session transcripts** and is gitignored because they contain work
  endpoints. Never commit it, and never copy its text into fixtures; rebuild the *shapes*
  instead.

## Spec deviations decided while planning

1. **`harness.target()` becomes two functions:** `run_key(harness, call)` (what groups calls
   into a run) and `call_paths(harness, call)` (the paths a call touches). The focus profile
   needs both separately.
2. **Two extra additive fields, `Message.claims_done` and `ParsedSession.final_text`,** so the
   recovery and alignment detectors see completion claims past the 200-char preview without
   file I/O.
3. **Tier strings are `S`, `S->J` and `J`, and "no fix target" is written `none`.** This keeps
   CLI flags ASCII.
4. **pi's `Request timed out.` API error classifies as `network`.**
5. **The drift candidate that `user-interrupt` produces cites up to 8 calls immediately before
   the interrupt,** within the interrupted turn.
6. **Error classes with fix target `none` are not surfaced as findings:** `aborted`,
   `sibling-cascade`, `user-rejected`, and the generic classes. `user-interrupt` covers aborts.
7. **The `hung` rule "last assistant message has an empty stop reason" applies to pi only.**
   Claude Code writes `stop_reason: null` on every intermediate per-block record, so on
   Claude Code sessions the rule would fire constantly.

## Review Focus

1. **Claude Code multi-record assistant turns.** Claude Code writes one record per content
   block, and most of those records have `stop_reason: null`. They must not fire `hung`,
   `empty-turn` or `generation-stall` (tests in Tasks 8 and 10).
2. **Empty or mixed-format timestamps.** A message or tool result with an empty timestamp,
   or with `+00:00` instead of `Z`, must be skipped by the latency and interference checks,
   never cause an exception (tests in Tasks 3, 8 and 13).
3. **A tool input cut off by the 2,000-char `input_preview` cap** is invalid JSON. Path and
   command extraction must fall back to a regex, not raise (test in Task 2).
4. **A header-only session with no messages or tools** must give `detect()` → `[]` and
   `derive_end_state()` → `unknown` (test in Task 13).
5. **A pi-subagents run folder with a missing or malformed `meta.json`, or no
   `session_info` name,** must still load as a subagent linked to its parent (test in
   Task 6).

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

---

### Task 3: `features.py`, the parse-time text features

**Files:**
- Create: `sessionkit/features.py`
- Test: `tests/test_features.py` (new)

**Interfaces:**
- Produces:
  - `split_content(content, tool_block_type) -> tuple[str, str, int]`, returning
    (visible, reasoning, tool-call count)
  - `markup_leak(text) -> str`: one of `tool_call-xml`, `function-tag`, `harmony`,
    `json-call`, or `""`
  - `repeat_ratio(text) -> float`
  - `extract_refs(text) -> list[str]`
  - `claims_done(text) -> bool`
  - `CLAIM_RE`
  - `parse_ts(ts) -> datetime | None`
  - `delta_ms(start, end) -> int | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_features.py`:

```python
"""Table-driven tests for the parse-time text features."""

from __future__ import annotations

import unittest

from sessionkit import features


class SplitContentTest(unittest.TestCase):
    def test_string_content_is_visible(self) -> None:
        self.assertEqual(features.split_content("hi", "tool_use"), ("hi", "", 0))

    def test_blocks_split_into_visible_reasoning_and_calls(self) -> None:
        blocks = [{"type": "thinking", "thinking": "hmm"},
                  {"type": "text", "text": "answer"},
                  {"type": "tool_use", "id": "t", "name": "Read", "input": {}}]
        self.assertEqual(features.split_content(blocks, "tool_use"), ("answer", "hmm", 1))

    def test_only_the_named_tool_block_type_counts(self) -> None:
        blocks = [{"type": "toolCall", "id": "c", "name": "read", "arguments": {}}]
        self.assertEqual(features.split_content(blocks, "toolCall")[2], 1)
        self.assertEqual(features.split_content(blocks, "tool_use")[2], 0)

    def test_non_list_content(self) -> None:
        self.assertEqual(features.split_content(None, "tool_use"), ("", "", 0))


class MarkupLeakTest(unittest.TestCase):
    CASES = [
        ('<tool_call>\n{"name": "read", "arguments": {}}\n</tool_call>', "tool_call-xml"),
        ("<function=read>\n<parameter=path>/a</parameter>\n</function>", "function-tag"),
        ("<|channel|>commentary to=functions.read <|constrain|>json", "harmony"),
        ('{"name": "bash", "arguments": {"command": "ls"}}', "json-call"),
        ('Use a call like {"name": "bash", "arguments": {}} to run it.', ""),
        ("```\n<tool_call>{}</tool_call>\n```", ""),
        ("", ""),
    ]

    def test_cases(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(text=text[:30]):
                self.assertEqual(features.markup_leak(text), expected)


class RepeatRatioTest(unittest.TestCase):
    def test_short_text_is_not_judged(self) -> None:
        self.assertEqual(features.repeat_ratio("one two three"), 0.0)

    def test_distinct_text_scores_zero(self) -> None:
        self.assertEqual(features.repeat_ratio(" ".join(f"w{i}" for i in range(200))), 0.0)

    def test_a_restated_plan_scores_high(self) -> None:
        sentence = "Now I am setting up the work profile settings file with the defaults. "
        self.assertGreater(features.repeat_ratio(sentence * 30), 0.8)

    def test_large_input_is_handled(self) -> None:
        text = "alpha beta gamma delta " * 3000
        self.assertGreater(features.repeat_ratio(text), 0.9)


class ExtractRefsTest(unittest.TestCase):
    def test_backticks_paths_and_filenames(self) -> None:
        refs = features.extract_refs(
            "implement @docs/superpowers/plans/x.md using `run_key` in harness.py")
        self.assertEqual(refs, ["run_key", "docs/superpowers/plans/x.md", "harness.py"])

    def test_urls_are_not_refs(self) -> None:
        self.assertEqual(features.extract_refs("see https://pi.dev/docs/latest/models"), [])

    def test_deduplicated_and_capped(self) -> None:
        text = " ".join(f"f{i}.py" for i in range(40)) + " f1.py"
        refs = features.extract_refs(text)
        self.assertEqual(len(refs), 30)
        self.assertEqual(len(set(refs)), 30)


class ClaimsDoneTest(unittest.TestCase):
    def test_claims(self) -> None:
        for text in ("All tests pass.", "The fix is done and verified.", "Done.",
                     "✅ implemented"):
            with self.subTest(text):
                self.assertTrue(features.claims_done(text))

    def test_not_claims(self) -> None:
        for text in ("Let me check if this works.", "Next I'll run the tests.", ""):
            with self.subTest(text):
                self.assertFalse(features.claims_done(text))


class TimestampTest(unittest.TestCase):
    def test_delta(self) -> None:
        self.assertEqual(features.delta_ms("2026-09-26T01:00:00.000Z",
                                           "2026-09-26T01:00:02.500Z"), 2500)

    def test_mixed_offset_formats(self) -> None:
        self.assertEqual(features.delta_ms("2026-09-26T01:00:00+00:00",
                                           "2026-09-26T01:00:01.000Z"), 1000)

    def test_unusable_timestamps(self) -> None:
        self.assertIsNone(features.delta_ms("", "2026-09-26T01:00:00Z"))
        self.assertIsNone(features.delta_ms("garbage", "2026-09-26T01:00:00Z"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_features -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sessionkit.features'`

- [ ] **Step 3: Create `sessionkit/features.py`**

```python
"""Features parsers extract once from full message text, so detectors never re-read files.

Both parsers call these helpers, which keeps Claude Code and pi sessions comparable: the same
leak scan, repetition measure, reference extraction and completion-claim check run over each
harness's full text, not the 200-char preview.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_FENCE = re.compile(r"```.*?```", re.S)
_LEAKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tool_call-xml", re.compile(r"</?tool_call>|\[TOOL_CALLS\]")),
    ("function-tag", re.compile(r"<function=[\w.-]+>|<parameter=[\w.-]+>")),
    ("harmony", re.compile(r"<\|channel\|>[^\n]{0,80}?to=functions\.|<\|call\|>")),
    ("json-call", re.compile(
        r'^\s*\{\s*"name"\s*:\s*"[\w.-]+"\s*,\s*"(?:arguments|parameters)"\s*:', re.M)),
)

#: A *claim* that the work is finished — stricter than classify's completion regex, which
#: also fires on every sentence containing "works" or "ready".
CLAIM_RE = re.compile(
    r"\b(?:all (?:the )?tests? pass(?:ed|es|ing)?|tests? (?:now )?pass(?:es|ing)?"
    r"|(?:is|are|now) (?:done|complete|fixed|working)|successfully"
    r"|task (?:is )?complete|everything (?:works|is working))\b"
    r"|(?m:^\s*(?:all )?done[.!]?\s*$)|✅", re.I)

_REF_BACKTICK = re.compile(r"`([^`\n]{2,120})`")
_REF_PATH = re.compile(r"(?<![\w/])@?((?:~|\.{1,2})?/?[\w.-]+(?:/[\w.-]+)+/?)")
_REF_FILE = re.compile(
    r"\b[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|md|json|jsonl|ya?ml|toml|sh|rs|go|java|kt|rb|"
    r"c|h|cpp|hpp|cs|txt|cfg|ini|html|css|sql|lock|env)\b")
_MAX_REFS = 30
_NGRAM = 8
_MIN_GRAMS = 50


def split_content(content: Any, tool_block_type: str) -> tuple[str, str, int]:
    """Split an assistant content field into (visible text, reasoning text, tool-call count).

    Args:
        content: A plain string or a list of content blocks.
        tool_block_type: The block ``type`` this harness uses for a tool call
            (``tool_use`` for Claude Code, ``toolCall`` for pi).
    """
    if isinstance(content, str):
        return content, "", 0
    if not isinstance(content, list):
        return "", "", 0
    visible: list[str] = []
    reasoning: list[str] = []
    calls = 0
    for block in content:
        if isinstance(block, str):
            visible.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == tool_block_type:
            calls += 1
        elif kind in ("thinking", "redacted_thinking"):
            reasoning.append(str(block.get("thinking") or ""))
        elif kind == "text":
            visible.append(str(block.get("text") or ""))
    return "\n".join(v for v in visible if v), "\n".join(r for r in reasoning if r), calls


def markup_leak(text: str) -> str:
    """Name the tool-call markup found in plain text, or ``""``.

    Fenced code is ignored: an agent explaining a tool-call format is not leaking one, and a
    JSON call counts only when it starts its own line, not as an example inside prose.
    """
    if not text:
        return ""
    unfenced = _FENCE.sub("", text)
    for name, pattern in _LEAKS:
        if pattern.search(unfenced):
            return name
    return ""


def repeat_ratio(text: str) -> float:
    """``1 - distinct/total`` over word 8-grams; 0.0 below 50 grams (too short to judge)."""
    words = text.split()
    total = len(words) - _NGRAM + 1
    if total < _MIN_GRAMS:
        return 0.0
    distinct = {tuple(words[i:i + _NGRAM]) for i in range(total)}
    return round(1 - len(distinct) / total, 3)


def extract_refs(text: str) -> list[str]:
    """Paths, backticked identifiers and filenames a user message names, first-seen order."""
    found: list[str] = []
    for pattern in (_REF_BACKTICK, _REF_PATH):
        for match in pattern.finditer(text):
            _add_ref(found, match.group(1))
    for match in _REF_FILE.finditer(text):
        name = match.group(0)
        if not any(r.endswith("/" + name) for r in found):
            _add_ref(found, name)
    return found[:_MAX_REFS]


def _add_ref(found: list[str], raw: str) -> None:
    ref = raw.strip().rstrip(".,:;")
    if ref and ref not in found:
        found.append(ref)


def claims_done(text: str) -> bool:
    """Whether a message claims the work is finished."""
    return bool(text) and CLAIM_RE.search(text) is not None


def parse_ts(ts: str) -> datetime | None:
    """An ISO-8601 timestamp as an aware datetime, or ``None`` if it is empty or unusable."""
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def delta_ms(start: str, end: str) -> int | None:
    """Milliseconds between two ISO-8601 timestamps, or ``None`` if either is unusable."""
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m unittest tests.test_features -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add sessionkit/features.py tests/test_features.py
git commit -m "Add parse-time text features: content split, leaks, repetition, refs"
```

---
### Task 4: Data-model fields, populated by the Claude Code parser

**Files:**
- Modify: `sessionkit/parse.py`. This adds the `Message`/`ParsedSession` fields, changes
  `_assistant`, `_user`, `_system` and `parse_file`, and introduces new constants.
- Modify: `sessionkit/classify.py` and `sessionkit/detectors/lifecycle.py`: the compaction
  subtype becomes `compaction`.
- Modify: `sessionkit/query.py` (`tail_context` scans visible text only).
- Modify: `tests/fixtures.py` (`assistant()` gains `stop_reason`).
- Modify: `tests/test_classify.py`: the three `SysEvent(..., "compact_boundary")` literals
  become `"compaction"`. This is an **intended change**.
- Test: `tests/test_parse.py` (new class `ParseFeaturesTest`)

**Interfaces:**
- Consumes: `features.split_content`, `markup_leak`, `repeat_ratio`, `extract_refs` and
  `claims_done` (Task 3).
- Produces:
  - `Message` fields: `stop_reason`, `tool_calls`, `reasoning_len`, `error_text`,
    `markup_leak`, `repeat_ratio`, `refs`, `claims_done`
  - `ParsedSession` fields: `last_line_malformed` and `final_text`
  - `parse.ERROR_PREVIEW = 300` and `parse.FINAL_TEXT_CHARS = 4000`
  - The normalized sysevent subtype `compaction`
- Stop-reason vocabulary: `end`, `tool_use`, `length`, `error`, `aborted`, `""`.

- [ ] **Step 1: Let the fixture set a stop reason**

In `tests/fixtures.py`, replace `assistant()` with:

```python
def assistant(blocks: list[dict[str, Any]], ts: str = "2026-08-01T00:00:01Z",
              model: str = "claude-opus-5", usage: dict[str, int] | None = None,
              stop_reason: str | None = None, **extra: Any) -> dict[str, Any]:
    """An assistant turn carrying arbitrary content blocks."""
    message: dict[str, Any] = {"role": "assistant", "model": model, "content": blocks,
                               "usage": usage or {"input_tokens": 10, "output_tokens": 5}}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {"type": "assistant", "sessionId": SID, "cwd": CWD, "timestamp": ts,
            "uuid": f"a-{ts}", "message": message, **extra}
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_parse.py`, before the `if __name__ == "__main__":` line if the file
has one:

```python
class ParseFeaturesTest(unittest.TestCase):
    """Parse-time features: visible vs reasoning text, stop reasons, API errors, refs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parse(self, records: list[dict]) -> "object":
        return parse_file(fx.write(self.tmp, records), "host")

    def _last(self, *blocks: dict, **kw):
        session = self.parse([fx.user("go"), fx.assistant(list(blocks), **kw)])
        return session.messages[-1]

    def test_reasoning_is_not_visible_text(self) -> None:
        message = self._last({"type": "thinking", "thinking": "secret plan"},
                             {"type": "text", "text": "All done"}, stop_reason="end_turn")
        self.assertEqual(message.preview, "All done")
        self.assertEqual(message.text_len, len("All done"))
        self.assertEqual(message.reasoning_len, len("secret plan"))
        self.assertEqual(message.stop_reason, "end")

    def test_stop_reason_mapping(self) -> None:
        text = {"type": "text", "text": "x"}
        self.assertEqual(self._last(text, stop_reason="max_tokens").stop_reason, "length")
        self.assertEqual(self._last(text, stop_reason="tool_use").stop_reason, "tool_use")
        self.assertEqual(self._last(text).stop_reason, "")

    def test_api_error_message(self) -> None:
        message = self._last({"type": "text", "text": "API Error: Rate limit reached"},
                             stop_reason="stop_sequence", model="<synthetic>",
                             isApiErrorMessage=True, error="rate_limit")
        self.assertEqual(message.stop_reason, "error")
        self.assertIn("rate_limit", message.error_text)
        self.assertIn("Rate limit reached", message.error_text)

    def test_synthetic_model_does_not_replace_the_session_model(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([{"type": "text", "text": "ok"}], model="claude-opus-5"),
            fx.assistant([{"type": "text", "text": "API Error: 529"}],
                         ts="2026-08-01T00:00:05Z", model="<synthetic>",
                         isApiErrorMessage=True, error="server_error"),
        ])
        self.assertEqual(session.model, "claude-opus-5")

    def test_tool_calls_counted(self) -> None:
        message = self._last(fx.tool_use("t1", "Read", {"file_path": "/a"}),
                             fx.tool_use("t2", "Read", {"file_path": "/b"}))
        self.assertEqual(message.tool_calls, 2)

    def test_markup_leak_and_repeat_ratio(self) -> None:
        leak = self._last({"type": "text", "text": '<tool_call>{"name": "Read"}</tool_call>'})
        self.assertEqual(leak.markup_leak, "tool_call-xml")
        loop = self._last({"type": "thinking",
                           "thinking": "I will set up the profile file with defaults now. " * 30})
        self.assertGreater(loop.repeat_ratio, 0.5)

    def test_user_refs(self) -> None:
        session = self.parse([fx.user("fix `parse_file` in sessionkit/parse.py")])
        self.assertEqual(session.messages[0].refs, ["parse_file", "sessionkit/parse.py"])

    def test_compaction_is_normalised(self) -> None:
        session = self.parse([fx.user("go"),
                              fx.system("compact_boundary",
                                        compactMetadata={"trigger": "auto", "preTokens": 1000})])
        self.assertEqual([(e.subtype, e.detail) for e in session.sysev],
                         [("compaction", "auto:1000")])

    def test_last_line_malformed(self) -> None:
        path = fx.write(self.tmp, fx.simple_session())
        self.assertFalse(parse_file(path, "host").last_line_malformed)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "assist')
        self.assertTrue(parse_file(path, "host").last_line_malformed)

    def test_harness_final_text_and_claim(self) -> None:
        session = self.parse(fx.simple_session())
        self.assertEqual(session.harness, "claude-code")
        self.assertEqual(session.final_text, "done")
        self.assertTrue(session.messages[-1].claims_done)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse.ParseFeaturesTest -v`
Expected: FAIL. `Message` has no attribute `reasoning_len`, and the fixture keyword test
errors until the parser changes land.

- [ ] **Step 4: Add the fields and constants to `sessionkit/parse.py`**

Change the import `from sessionkit import pricing` to:

```python
from sessionkit import features, pricing
```

Directly after `OUTPUT_PREVIEW = 2000`, add:

```python
ERROR_PREVIEW = 300
#: The last assistant message's visible text, kept from its end, so tail-signal and
#: completion-claim checks see markers past the 200-char preview without re-reading the file.
FINAL_TEXT_CHARS = 4000
#: Claude Code ``stop_reason`` → the harness-neutral vocabulary (spec §4). ``null`` — normal on
#: the intermediate per-content-block records Claude Code writes — maps to "".
_CC_STOP_REASONS = {"end_turn": "end", "stop_sequence": "end", "pause_turn": "end",
                    "refusal": "end", "tool_use": "tool_use", "max_tokens": "length"}
#: The model name Claude Code writes on its own synthetic API-error messages.
_SYNTHETIC_MODEL = "<synthetic>"
```

In `class Message`, after `preview: str = ""`, add:

```python
    stop_reason: str = ""
    tool_calls: int = 0
    reasoning_len: int = 0
    error_text: str = ""
    markup_leak: str = ""
    repeat_ratio: float = 0.0
    refs: list[str] = field(default_factory=list)
    claims_done: bool = False
```

In `class ParsedSession`, after `tail_signal: str = ""`, add:

```python
    last_line_malformed: bool = False
    final_text: str = ""
```

In `_Parser.__init__`, replace the `self.out = ...` line with:

```python
        self.out = ParsedSession(sid=sid, source_id=source_id, path=str(path),
                                 harness="claude-code")
```

- [ ] **Step 5: Rewrite `_assistant`, and update `_user` and `_system`**

Replace `_Parser._assistant` with:

```python
    def _assistant(self, line_no: int, rec: dict[str, Any]) -> None:
        """Record an assistant turn: usage, visible text vs reasoning, and tool_use blocks."""
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        ts = str(rec.get("timestamp") or "")
        model = str(msg.get("model") or "")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        tok_in = int(usage.get("input_tokens") or 0)
        tok_out = int(usage.get("output_tokens") or 0)
        tok_cr = int(usage.get("cache_read_input_tokens") or 0)
        tok_cc = int(usage.get("cache_creation_input_tokens") or 0)

        out = self.out
        out.tok_in += tok_in
        out.tok_out += tok_out
        out.tok_cache_read += tok_cr
        out.tok_cache_create += tok_cc
        out.cost_usd += pricing.cost(model, tok_in, tok_out, tok_cr, tok_cc)
        if model and model != _SYNTHETIC_MODEL:
            out.model = model

        visible, reasoning, calls = features.split_content(msg.get("content"), "tool_use")
        stop = _CC_STOP_REASONS.get(str(msg.get("stop_reason") or ""), "")
        error_text = ""
        if rec.get("isApiErrorMessage"):
            stop = "error"
            error_text = preview(f"{rec.get('error') or 'api_error'}: {visible}", ERROR_PREVIEW)
        full = f"{visible}\n{reasoning}"
        out.messages.append(Message(
            uuid=str(rec.get("uuid") or ""), line=line_no, ts=ts, role="assistant",
            model=model, tok_in=tok_in, tok_out=tok_out, tok_cr=tok_cr, tok_cc=tok_cc,
            text_len=len(visible), preview=preview(visible, MSG_PREVIEW), stop_reason=stop,
            tool_calls=calls, reasoning_len=len(reasoning), error_text=error_text,
            markup_leak=features.markup_leak(full), repeat_ratio=features.repeat_ratio(full),
            claims_done=features.claims_done(visible),
        ))
        out.final_text = visible[-FINAL_TEXT_CHARS:]
        self._tool_uses(line_no, ts, msg.get("content"))
```

In `_Parser._user`, replace everything from `text = block_text(content)` to the end of the
method with:

```python
        text = block_text(content)
        prompt = not rec.get("isMeta") and bool(text.strip())
        if prompt:
            self.out.turns += 1
            if not self.out.first_prompt:
                self.out.first_prompt = preview(text, MSG_PREVIEW)
        self.out.messages.append(Message(
            uuid=str(rec.get("uuid") or ""), line=line_no, ts=ts, role="user",
            text_len=len(text), preview=preview(text, MSG_PREVIEW),
            refs=features.extract_refs(text) if prompt else [],
        ))
```

In `_Parser._system`, replace the `elif subtype == "compact_boundary":` branch with:

```python
        elif subtype == "compact_boundary":
            subtype = "compaction"
            meta = rec.get("compactMetadata")
            if isinstance(meta, dict):
                detail = f"{meta.get('trigger', '?')}:{meta.get('preTokens', 0)}"
```

- [ ] **Step 6: Track a malformed final line in `parse_file`**

In `parse.parse_file`:
1. Add `last_malformed = False` next to `agent_seen = ""`.
2. In **both** malformed branches (the `json.JSONDecodeError` one and the
   `not isinstance(rec, dict)` one), add `last_malformed = True` before `continue`.
3. Add `last_malformed = False` directly after the `if not isinstance(rec, dict):` block,
   before `sid_seen = ...`.
4. After `out = parser.finish()`, add:

```python
    out.last_line_malformed = last_malformed
```

- [ ] **Step 7: Switch the compaction consumers to the normalized subtype**

- In `sessionkit/classify.py`, `derive_end_state`: change
  `e.subtype == "compact_boundary"` to `e.subtype == "compaction"`.
- In `sessionkit/detectors/lifecycle.py`, `compaction_churn`: make the same change.
- In `tests/test_classify.py`: change the three `"compact_boundary"` string literals, in
  `test_compaction_churn` and `test_compaction_churn_negative_single`, to `"compaction"`.

In `sessionkit/query.py`, `tail_context`, replace:

```python
            text = parse.block_text(content) if content is not None else ""
```

with:

```python
            text = features.split_content(content, "")[0] if content is not None else ""
```

and add `from sessionkit import features` to `query.py`'s imports.

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/parse.py sessionkit/classify.py sessionkit/detectors/lifecycle.py \
  sessionkit/query.py tests/fixtures.py tests/test_parse.py tests/test_classify.py
git commit -m "Record stop reasons, reasoning and text features in Claude Code parsing"
```

---

### Task 5: The pi parser populates the same fields, and end state reads `stop_reason`

**Files:**
- Modify: `sessionkit/parse_pi.py` (`_Parser`, `_subagent_children`, `parse_file`)
- Modify: `sessionkit/classify.py`: `derive_end_state` uses the message's `stop_reason`, and
  `_final_api_error` is deleted.
- Modify: `sessionkit/query.py` (`timeline_rows` shows `error_text` for errored messages)
- Modify: `tests/fixtures_pi.py`: adds `thinking_block` and `context_edit`, and a `usage`
  parameter on `compaction`.
- Modify: `tests/test_parse_pi.py`: **delete**
  `test_assistant_error_stop_recorded_as_sysevent` and
  `test_normal_assistant_stop_records_no_api_error`. The `api_error` sysevent no longer
  exists, and `ParsePiFeaturesTest` replaces both. This is an **intended change**.
- Test: `tests/test_parse_pi.py` (new class `ParsePiFeaturesTest`), and
  `tests/test_classify.py` (one new end-state test)

**Interfaces:**
- Consumes: the Task 3 features and the Task 4 fields.
- Produces:
  - The pi stop-reason mapping: `stop`→`end`, `toolUse`→`tool_use`, `length`, `error` and
    `aborted` map to themselves, and anything else→`""`.
  - `Message.error_text`, set only when `stop_reason == "error"`.
  - A `context_edit` sysevent.
  - Compaction `usage` is added to the session's tokens and cost.
  - `harness="pi"` on every pi session, including synthetic children.

- [ ] **Step 1: Extend `tests/fixtures_pi.py`**

Add these builders. Replace the existing `compaction` builder with the version below; add the
other two anywhere after `_entry`:

```python
def thinking_block(text: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text}


def context_edit(entry_id: str, parent_id: str | None, target_id: str,
                 ts: str = "2026-09-01T00:00:04.000Z") -> dict[str, Any]:
    """A ``context_edit`` entry — seen in real pi 0.84 sessions, absent from its docs."""
    return _entry("context_edit", entry_id, parent_id, ts, targetId=target_id,
                  replacement=None)


def compaction(entry_id: str, parent_id: str | None, summary: str, tokens_before: int,
               ts: str = "2026-09-01T00:00:04.000Z",
               usage: dict[str, Any] | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {"usage": usage} if usage is not None else {}
    return _entry("compaction", entry_id, parent_id, ts, summary=summary,
                  tokensBefore=tokens_before, **extra)
```

- [ ] **Step 2: Write the failing tests**

In `tests/test_parse_pi.py`:
1. Delete the two tests named under **Files**.
2. Append this class before the `if __name__` line, or at the end of the file.

```python
class ParsePiFeaturesTest(unittest.TestCase):
    """pi messages carry the same parse-time features as Claude Code's."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parse(self, entries: list[dict]) -> "object":
        return parse_file(fx.write(self.tmp, entries), "pi")

    def _turn(self, content: list[dict], **kw) -> list[dict]:
        return [fx.header(), fx.user_msg("e1", None, "go"),
                fx.assistant_msg("e2", "e1", content, **kw)]

    def test_error_stop_is_recorded_on_the_message(self) -> None:
        session = self.parse(fx.api_error_session())
        last = session.messages[-1]
        self.assertEqual(last.stop_reason, "error")
        self.assertIn("Refresh token expired", last.error_text)
        self.assertFalse([e for e in session.sysev if e.subtype == "api_error"])

    def test_normal_stop_has_no_error_text(self) -> None:
        last = self.parse(fx.simple_session()).messages[-1]
        self.assertEqual(last.stop_reason, "end")
        self.assertEqual(last.error_text, "")

    def test_thinking_is_reasoning_not_visible_text(self) -> None:
        session = self.parse(self._turn([fx.thinking_block("private plan"),
                                         {"type": "text", "text": "Done."}]))
        last = session.messages[-1]
        self.assertEqual(last.preview, "Done.")
        self.assertEqual(last.reasoning_len, len("private plan"))
        self.assertTrue(last.claims_done)
        self.assertEqual(session.final_text, "Done.")

    def test_stop_reason_mapping(self) -> None:
        for raw, expected in (("length", "length"), ("aborted", "aborted"),
                              ("toolUse", "tool_use"), ("pending", "")):
            with self.subTest(raw):
                last = self.parse(self._turn([], stop_reason=raw)).messages[-1]
                self.assertEqual(last.stop_reason, expected)
                self.assertEqual(last.error_text, "")

    def test_context_edit_is_a_sysevent_not_malformed(self) -> None:
        session = self.parse([fx.header(), fx.user_msg("e1", None, "go"),
                              fx.context_edit("e2", "e1", "e1")])
        self.assertEqual(session.malformed_lines, 0)
        self.assertIn("context_edit", [e.subtype for e in session.sysev])

    def test_compaction_usage_is_counted(self) -> None:
        usage = {"input": 1000, "output": 200, "cost": {"total": 0.05}}
        session = self.parse([fx.header(), fx.user_msg("e1", None, "go"),
                              fx.compaction("e2", "e1", "summary", 90_000, usage=usage)])
        self.assertAlmostEqual(session.cost_usd, 0.05)
        self.assertEqual(session.tok_in, 1000)

    def test_harness_and_refs(self) -> None:
        session = self.parse([fx.header(), fx.user_msg("e1", None, "update @config/models.json")])
        self.assertEqual(session.harness, "pi")
        self.assertEqual(session.messages[0].refs, ["config/models.json"])

    def test_last_line_malformed(self) -> None:
        path = fx.write(self.tmp, fx.simple_session())
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "mess')
        self.assertTrue(parse_file(path, "pi").last_line_malformed)
```

In `tests/test_classify.py`, `EndStateTest`, add:

```python
    def test_final_error_turn_is_error_cascade_with_its_text(self) -> None:
        from sessionkit.parse import Message
        session = _session()
        session.messages.append(Message(uuid="u", line=3, ts="", role="assistant",
                                        stop_reason="error", error_text="rate_limit: slow down"))
        self.assertEqual(derive_end_state(session), ("error-cascade", "rate_limit: slow down"))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse_pi tests.test_classify -v`
Expected: FAIL. `stop_reason` is `""` for pi messages, `context_edit` counts as malformed,
and the end-state test gets `("complete", "")`.

- [ ] **Step 4: Update `sessionkit/parse_pi.py`**

Imports: add `import re` after `import json`, and change the parse import block to:

```python
from sessionkit import features
from sessionkit.parse import FileOp, Message, ParsedSession, SysEvent, ToolCall, digest
from sessionkit.redact import preview
```

After `PATH_KEY = "path"`, add:

```python
ERROR_PREVIEW = 300
FINAL_TEXT_CHARS = 4000
#: pi ``stopReason`` → the harness-neutral vocabulary; ``pending``/``deferred`` map to "".
_PI_STOP_REASONS = {"stop": "end", "toolUse": "tool_use", "length": "length",
                    "error": "error", "aborted": "aborted"}
```

In `_Parser.__init__`, replace the `ParsedSession(...)` construction with:

```python
        self.out = session if session is not None else ParsedSession(
            sid=sid, source_id=source_id, path=str(path), harness="pi")
```

In `_Parser.feed`, change the first sysevent branch's tuple to include `context_edit`:

```python
        elif etype in ("model_change", "thinking_level_change", "label", "session_info",
                       "context_edit"):
```

and replace the `elif etype == "compaction":` branch with:

```python
        elif etype == "compaction":
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self._compaction_usage(rec.get("usage"))
            self.out.sysev.append(
                SysEvent(line_no, ts, "compaction", str(rec.get("tokensBefore") or "")))
```

Add this method to `_Parser`, after `_header`:

```python
    def _compaction_usage(self, usage: Any) -> None:
        """Count the model call that wrote a compaction summary — it is billed like any turn."""
        if not isinstance(usage, dict):
            return
        self.out.tok_in += int(usage.get("input") or 0)
        self.out.tok_out += int(usage.get("output") or 0)
        cost = usage.get("cost")
        total = cost.get("total") if isinstance(cost, dict) else cost
        self.out.cost_usd += float(total or 0.0)
```

Replace `_Parser._user` with:

```python
    def _user(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        text = _text_of(msg.get("content"))
        out = self.out
        prompt = bool(text.strip())
        if prompt:
            out.turns += 1
            if not out.first_prompt:
                out.first_prompt = preview(text, MSG_PREVIEW)
        out.messages.append(Message(uuid=f"u-{line_no}", line=line_no, ts=ts, role="user",
                                    text_len=len(text), preview=preview(text, MSG_PREVIEW),
                                    refs=features.extract_refs(text) if prompt else []))
```

Replace `_Parser._assistant` with:

```python
    def _assistant(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        out = self.out
        model = str(msg.get("model") or "")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        tok_in = int(usage.get("input") or 0)
        tok_out = int(usage.get("output") or 0)
        tok_cr = int(usage.get("cacheRead") or 0)
        tok_cc = int(usage.get("cacheWrite") or 0)
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        out.tok_in += tok_in
        out.tok_out += tok_out
        out.tok_cache_read += tok_cr
        out.tok_cache_create += tok_cc
        out.cost_usd += float(cost.get("total") or 0.0)
        if model:
            out.model = model

        content = msg.get("content")
        visible, reasoning, calls = features.split_content(content, "toolCall")
        stop = _PI_STOP_REASONS.get(str(msg.get("stopReason") or ""), "")
        error_text = (preview(str(msg.get("errorMessage") or ""), ERROR_PREVIEW)
                      if stop == "error" else "")
        full = f"{visible}\n{reasoning}"
        out.messages.append(Message(
            uuid=f"a-{line_no}", line=line_no, ts=ts, role="assistant", model=model,
            tok_in=tok_in, tok_out=tok_out, tok_cr=tok_cr, tok_cc=tok_cc,
            text_len=len(visible), preview=preview(visible, MSG_PREVIEW), stop_reason=stop,
            tool_calls=calls, reasoning_len=len(reasoning), error_text=error_text,
            markup_leak=features.markup_leak(full), repeat_ratio=features.repeat_ratio(full),
            claims_done=features.claims_done(visible)))
        out.final_text = visible[-FINAL_TEXT_CHARS:]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._open_tool_call(line_no, ts, block)
```

In `_subagent_children`, add `harness="pi"` to the child's `ParsedSession(...)` call.

In `parse_file`, make these changes:
1. Add `last_malformed = False` before the `try:` that opens the file.
2. In both malformed branches, add `last_malformed = True` before `continue`.
3. Add `last_malformed = False` right after the `if not isinstance(rec, dict):` block.
4. Replace the final `return parser.finish()` with:

```python
    out = parser.finish()
    out.last_line_malformed = last_malformed
    return out
```

- [ ] **Step 5: `derive_end_state` reads the message**

In `sessionkit/classify.py`, delete `_final_api_error`, remove `SysEvent` from the
`sessionkit.parse` import, and in `derive_end_state` replace:

```python
        api_error = _final_api_error(session)
        if api_error is not None:
            return ("error-cascade", api_error.detail or "final model turn ended in an API error")
```

with:

```python
        last = session.messages[-1]
        if last.stop_reason == "error":
            return ("error-cascade", last.error_text or "final model turn ended in an API error")
```

- [ ] **Step 6: Keep API errors visible in the timeline**

In `sessionkit/query.py`, `timeline_rows`, replace the message loop with:

```python
    for message in session.messages:
        detail = message.preview
        if not detail and message.error_text:
            detail = f"[{message.stop_reason}] {message.error_text}"
        rows.append({"line": message.line, "kind": "msg", "name": message.role,
                     "detail": detail})
```

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS. `test_corpus_pi.test_session_ending_on_api_error_is_not_complete` still
passes, now via `error_text`.

- [ ] **Step 8: Commit**

```bash
git add sessionkit/parse_pi.py sessionkit/classify.py sessionkit/query.py \
  tests/fixtures_pi.py tests/test_parse_pi.py tests/test_classify.py
git commit -m "Parse pi stop reasons, reasoning and compaction cost; end state reads them"
```

---

### Task 6: Load pi-subagents async child runs and link them to their parent

**Files:**
- Modify: `sessionkit/parse_pi.py`: `session_info` keeps its `name`, and `subagent` results
  record `run:<runId>` edges. Also adds `async_run_identity`.
- Modify: `sessionkit/corpus.py` (`_pi_transcripts`, `load_one`, new helpers, `load`)
- Modify: `tests/fixtures_pi.py` (`session_info`, `async_dispatch_result`, `write_async_run`,
  `PARENT_FILE`)
- Test: `tests/test_parse_pi.py` (two tests), `tests/test_corpus_pi.py` (new class
  `PiAsyncRunTest`)

**Interfaces:**
- Consumes: `ParsedSession.dispatch_edges`, `is_subagent`, `parent_sid` and `agent_type`, and
  `query.children_rows`.
- Produces:
  - `parse_pi.async_run_identity(session) -> tuple[str, str]`, returning `(agent, run_id)`
  - `corpus._load_pi(source, path, dir_name) -> list[Loaded]`
  - `corpus._link_async_runs(sessions)`
  - Discovery of `*/*/run-*/session.jsonl` under a pi project folder

- [ ] **Step 1: Extend `tests/fixtures_pi.py`**

```python
PARENT_FILE = f"20260901T000000_{SID}.jsonl"


def session_info(entry_id: str, parent_id: str | None, name: str,
                 ts: str = "2026-09-01T00:00:00.500Z") -> dict[str, Any]:
    return _entry("session_info", entry_id, parent_id, ts, name=name)


def async_dispatch_result(entry_id: str, parent_id: str | None, call_id: str, agent: str,
                          run_id: str, ts: str = "2026-09-01T00:00:03.000Z") -> dict[str, Any]:
    """The toolResult pi-subagents writes for an async dispatch (shape from a real run)."""
    text = (f"Run fan-out: 1/64 used, 63 remaining\nAsync: {agent} [{run_id}]\n\n"
            "The async run is detached and running in the background.")
    return tool_result_msg(entry_id, parent_id, call_id, "subagent", text, ts=ts)


def write_async_run(tmp: Path, parent_name: str, folder_id: str,
                    entries: list[dict[str, Any]], *, run_id: str = "", agent: str = "",
                    meta: dict[str, Any] | None = None, meta_text: str | None = None) -> Path:
    """Write an async child run at ``<parent-stem>/<folder>/run-0/session.jsonl``, plus its
    ``subagent-artifacts/<runId>_<agent>_meta.json`` when ``meta``/``meta_text`` is given."""
    project = tmp / "sessions" / "--home-dev-myproject--"
    path = project / Path(parent_name).stem / folder_id / "run-0" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    if meta is not None or meta_text is not None:
        artifacts = project / "subagent-artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        body = meta_text if meta_text is not None else json.dumps(meta)
        (artifacts / f"{run_id}_{agent}_meta.json").write_text(body, encoding="utf-8")
    return path
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_parse_pi.py`, inside `ParsePiFeaturesTest`:

```python
    RUN = "743aeb2e-f120-434a-acfe-f879474708a7"

    def test_async_dispatch_records_a_run_edge(self) -> None:
        session = self.parse([
            fx.header(), fx.user_msg("e1", None, "go"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block("c1", "subagent", {"agent": "dev"})],
                             stop_reason="toolUse"),
            fx.async_dispatch_result("e3", "e2", "c1", "dev", self.RUN),
        ])
        self.assertIn(("c1", f"run:{self.RUN}"), session.dispatch_edges)

    def test_async_run_identity(self) -> None:
        from sessionkit.parse_pi import async_run_identity
        session = self.parse([fx.header(),
                              fx.session_info("i1", None, f"subagent-dev-qwen38-{self.RUN}-1")])
        self.assertEqual(async_run_identity(session), ("dev-qwen38", self.RUN))
```

Append to `tests/test_corpus_pi.py`:

```python
RUN_ID = "743aeb2e-f120-434a-acfe-f879474708a7"
CHILD_SID = "bbbb2222-cccc-3333-dddd-4444eeee5555"
FOLDER = "446b4ce2-fae8-45de-8023-27998b6049e4"


class PiAsyncRunTest(unittest.TestCase):
    """pi-subagents async runs: separate files beside the parent, linked by run id."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pi_home = self.tmp / "pi-agent"
        self.pi_home.mkdir()
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.tmp / "claude"), "PI_DIR": str(self.pi_home),
            "SESSIONKIT_ENV": str(env_file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, named: bool = True, meta: dict | None = None,
               meta_text: str | None = None) -> None:
        fx.write(self.pi_home, [
            fx.header(), fx.user_msg("e1", None, "implement task 1"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                "c1", "subagent", {"agent": "developer", "task": "t1"})], stop_reason="toolUse"),
            fx.async_dispatch_result("e3", "e2", "c1", "developer", RUN_ID),
            fx.assistant_msg("e4", "e3", [{"type": "text", "text": "dispatched"}],
                             ts="2026-09-01T00:00:05.000Z"),
        ])
        child = [fx.header(sid=CHILD_SID)]
        if named:
            child.append(fx.session_info("i1", None, f"subagent-developer-{RUN_ID}-1"))
        child += [fx.user_msg("e1", None, "Task 1"),
                  fx.assistant_msg("e2", "e1", [{"type": "text", "text": "finished"}])]
        fx.write_async_run(self.pi_home, fx.PARENT_FILE, FOLDER, child, run_id=RUN_ID,
                           agent="developer", meta=meta, meta_text=meta_text)

    def _load(self):
        loaded = corpus.load()
        return loaded, {e.session.sid: e for e in loaded.sessions}

    def test_async_child_is_loaded_and_linked(self) -> None:
        self._write(meta={"runId": RUN_ID, "agent": "developer", "exitCode": 0})
        loaded, by_sid = self._load()
        child = by_sid[CHILD_SID].session
        self.assertTrue(child.is_subagent)
        self.assertEqual(child.parent_sid, fx.SID)
        self.assertEqual(child.agent_type, "developer")
        self.assertEqual(child.end_state, "complete")
        parent = by_sid[fx.SID]
        self.assertIn(("c1", CHILD_SID), parent.session.dispatch_edges)
        rows = query.children_rows(parent, loaded)
        self.assertTrue(rows[0]["resolved"])
        self.assertEqual(rows[0]["child_sid"], CHILD_SID)

    def test_nonzero_exit_code_marks_the_run_failed(self) -> None:
        self._write(meta={"runId": RUN_ID, "agent": "developer", "exitCode": 1})
        child = self._load()[1][CHILD_SID].session
        self.assertEqual(child.end_state, "error-cascade")
        self.assertIn("code 1", child.end_reason)

    def test_run_without_meta_or_name_still_loads_as_a_subagent(self) -> None:
        self._write(named=False)
        by_sid = self._load()[1]
        child = by_sid[CHILD_SID].session
        self.assertTrue(child.is_subagent)
        self.assertEqual(child.parent_sid, fx.SID)
        self.assertEqual(child.agent_type, "")
        self.assertIn(("c1", f"run:{RUN_ID}"), by_sid[fx.SID].session.dispatch_edges)

    def test_malformed_meta_is_ignored(self) -> None:
        self._write(meta_text="{not json")
        child = self._load()[1][CHILD_SID].session
        self.assertEqual(child.agent_type, "developer")
        self.assertEqual(child.end_state, "complete")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse_pi tests.test_corpus_pi -v`
Expected: FAIL. There's no `run:` edge, `async_run_identity` is missing, and the child run
isn't discovered, so the `KeyError` on `CHILD_SID` fires.

- [ ] **Step 4: Record run identity and dispatch edges in `parse_pi.py`**

After `_PI_STOP_REASONS`, add:

```python
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: pi-subagents' async dispatch result names the detached run: ``Async: <agent> [<runId>]``.
_ASYNC_DISPATCH_RE = re.compile(rf"Async: \S+ \[({_UUID})\]")
#: An async child run names itself ``subagent-<agent>-<runId>-<n>`` in its session_info.
_ASYNC_NAME_RE = re.compile(rf"^subagent-(.+)-({_UUID})")
```

In `_Parser.feed`, remove `"session_info"` from the sysevent tuple and add this branch after
that tuple's branch:

```python
        elif etype == "session_info":
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(SysEvent(line_no, ts, "session_info",
                                           preview(str(rec.get("name") or ""), 200)))
```

In `_Parser._tool_result`, at the end of the `if call.name == "subagent":` block (after
`self.out.dispatch_edges.extend(...)`), add:

```python
            for run_id in _ASYNC_DISPATCH_RE.findall(body):
                self.out.dispatch_edges.append((call.tool_use_id, f"run:{run_id}"))
```

Add this module-level function after `_subagent_children`:

```python
def async_run_identity(session: ParsedSession) -> tuple[str, str]:
    """``(agent, run_id)`` from an async child run's ``session_info`` name, else ``("", "")``."""
    for event in session.sysev:
        if event.subtype == "session_info":
            match = _ASYNC_NAME_RE.match(event.detail)
            if match:
                return match.group(1), match.group(2)
    return "", ""
```

- [ ] **Step 5: Discover and link async runs in `corpus.py`**

Add `import json` and `from typing import Any` to the imports.

In `_pi_transcripts`, replace `files = sorted(project_dir.glob("*.jsonl"))` with:

```python
            files = sorted(project_dir.glob("*.jsonl"))
            files.extend(sorted(project_dir.glob("*/*/run-*/session.jsonl")))
```

Replace the `if source.kind == "pi":` block in `load_one` with
`return _load_pi(source, path, dir_name)` placed as that block's only line:

```python
    if source.kind == "pi":
        return _load_pi(source, path, dir_name)
```

and add these functions after `load_one`:

```python
def _load_pi(source: src.Source, path: Path, dir_name: str) -> list[Loaded]:
    """A pi session plus its synthetic subagent children, or one pi-subagents async run."""
    parent = parse_pi.parse_file(path, source.id)
    meta = _mark_async_child(parent, path) if _is_async_run(path) else {}
    annotate_errors(parent)
    parent.end_state, parent.end_reason = derive_end_state(parent)
    _apply_exit_code(parent, meta)
    loaded = [Loaded(parent, project_key(parent.cwd, source.id, dir_name), detect(parent))]
    for child in parent.child_sessions:
        annotate_errors(child)
        # end_state/end_reason were already assigned in parse_pi from the subagent result's
        # exitCode/stopReason — derive_end_state assumes a live tail of tool calls that a
        # synthetic child, built from an already-finished subprocess, does not have.
        loaded.append(Loaded(child, project_key(child.cwd, source.id, dir_name),
                             detect(child)))
    return loaded


def _is_async_run(path: Path) -> bool:
    """A pi-subagents async child run: ``<parent-stem>/<folder>/run-<n>/session.jsonl``."""
    return path.name == "session.jsonl" and path.parent.name.startswith("run-")


def _mark_async_child(session: ParsedSession, path: Path) -> dict[str, Any]:
    """Mark an async run as a subagent of the session its grandparent folder is named after.

    Returns the run's ``meta.json`` (empty when missing or unreadable) for the exit code.
    """
    parent_stem = path.parents[2].name
    session.is_subagent = True
    session.parent_sid = parent_stem.split("_", 1)[1] if "_" in parent_stem else parent_stem
    agent, run_id = parse_pi.async_run_identity(session)
    meta = _async_meta(path.parents[3] / "subagent-artifacts", run_id)
    session.agent_type = agent or str(meta.get("agent") or "")
    return meta


def _async_meta(folder: Path, run_id: str) -> dict[str, Any]:
    """The first readable ``<runId>_*_meta.json`` in ``folder``, or ``{}``."""
    if not run_id:
        return {}
    for candidate in sorted(folder.glob(f"{run_id}_*_meta.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _apply_exit_code(session: ParsedSession, meta: dict[str, Any]) -> None:
    """A run that exited non-zero did not complete, whatever its transcript's tail says."""
    code = meta.get("exitCode")
    if isinstance(code, bool) or not isinstance(code, int) or code == 0:
        return
    if session.end_state == "complete":
        session.end_state = "error-cascade"
        session.end_reason = f"subagent run exited with code {code}"


def _link_async_runs(sessions: list[Loaded]) -> None:
    """Resolve a parent's ``run:<runId>`` dispatch edges to the async child's session id."""
    runs: dict[str, str] = {}
    for entry in sessions:
        if entry.session.is_subagent:
            _agent, run_id = parse_pi.async_run_identity(entry.session)
            if run_id:
                runs[run_id] = entry.session.sid
    for entry in sessions:
        edges = entry.session.dispatch_edges
        if any(child.startswith("run:") for _call, child in edges):
            entry.session.dispatch_edges = [
                (call, runs.get(child[4:], child) if child.startswith("run:") else child)
                for call, child in edges]
```

In `load()`, add `_link_async_runs(corpus.sessions)` right after
`_disambiguate(corpus.sessions)`.

Update the `transcripts()` docstring's pi sentence to:
`pi has no projects/ indirection: sessions/<project>/<file>.jsonl directly, plus
pi-subagents async runs at sessions/<project>/<parent-stem>/<folder>/run-<n>/session.jsonl.`

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sessionkit/parse_pi.py sessionkit/corpus.py tests/fixtures_pi.py \
  tests/test_parse_pi.py tests/test_corpus_pi.py
git commit -m "Load pi-subagents async runs as subagents linked to their parent"
```

---
### Task 7: New and widened error classes, and API-error classification

**Files:**
- Modify: `sessionkit/taxonomy.py`:
  - replace `ERROR_CLASSES`;
  - add `_PI_EXIT_RE`, `API_ERROR_CLASSES` and `classify_api_error`;
  - add the API classes to `ALL_CLASSES`.
- Modify: `tests/test_classify.py`. In `TaxonomyTest.CASES`, the expected class for the
  `InputValidationError` string becomes `"invalid-args"`, because `input-validation` is
  renamed. This is an **intended change**.
- Test: `tests/test_taxonomy.py` (new classes `ErrorClassTest` and `ApiErrorTest`)

**Interfaces:**
- Produces the error classes, in this order: `hook-block`, `user-rejected`,
  `policy-denied`, `tool-error-structured`, `args-dropped`, `invalid-args`, `unknown-tool`,
  `truncated-call`, `edit-mismatch`, `tool-misuse`, `sibling-cascade`, `aborted`,
  `file-too-large`, `read-truncated`, `stale-read`, `unread-file`, `missing-tool`,
  `permission-denied`, `not-a-repo`, `not-found`, `rate-limit`, `api-error`, `mcp-error`,
  `timeout`, `exit-code`.
- Produces `classify_api_error(text) -> str`. It returns `context-overflow`, `auth`,
  `rate-limit`, `network` or `api-error`, and `""` for empty text.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_taxonomy.py` (before the `if __name__` line):

```python
class ErrorClassTest(unittest.TestCase):
    """Exact strings from pi 0.84.1's source and from the local Claude Code corpus."""

    CASES = [
        ('{"ok": false, "errors": [["description", "too short"]]}', "tool-error-structured"),
        ('Validation failed for tool "edit":\n  - path: required\n\nReceived arguments:\n{}',
         "args-dropped"),
        ('Validation failed for tool "edit":\n  - edits: required\n\nReceived arguments:\n'
         '{"path": "/a"}', "invalid-args"),
        ("MCP error -32602: Input validation error: Invalid arguments for tool",
         "invalid-args"),
        ("Tool bash_exec not found", "unknown-tool"),
        ("Unknown skill: brainstorm", "unknown-tool"),
        ('Tool call "write" was not executed: the response hit the output token limit, so '
         "its arguments may be truncated.", "truncated-call"),
        ("Could not find the exact text in /a.py. The old text must match exactly including "
         "all whitespace and newlines.", "edit-mismatch"),
        ("Found 3 occurrences of the text in /a.py. The text must be unique.", "edit-mismatch"),
        ("<tool_use_error>String to replace not found in file.", "edit-mismatch"),
        ("edits[0] and edits[1] overlap in /a.py. Merge them into one edit.", "edit-mismatch"),
        ("EISDIR: illegal operation on a directory, read", "tool-misuse"),
        ("Offset 900 is beyond end of file (120 lines total)", "tool-misuse"),
        ("Sibling tool call errored", "sibling-cascade"),
        ("[Request interrupted by user for tool use]", "aborted"),
        ("partial output\n\nCommand aborted", "aborted"),
        ("File is in a directory that is denied by your permission settings.", "policy-denied"),
        ("Tool execution was blocked", "policy-denied"),
        ("The user doesn't want to take this action right now.", "user-rejected"),
        ("MCP error: No active transport", "mcp-error"),
        ("some output\n\nCommand exited with code 1", "exit-code"),
        ("killed\n\nCommand exited with code 137", "killed"),
        ("bash: foo: command not found\n\nCommand exited with code 127", "missing-tool"),
        ("Command timed out after 30 seconds", "timeout"),
    ]

    def test_cases(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(expected=expected, text=text[:40]):
                self.assertEqual(taxonomy.classify_error(text)[0], expected)


class ApiErrorTest(unittest.TestCase):
    """Model-call failures recorded on the assistant message, not on a tool result."""

    CASES = [
        ("rate_limit: You've hit your session limit · resets 6:40pm", "rate-limit"),
        ("authentication_failed: Please run /login · API Error: 401", "auth"),
        ("OAuth refresh failed for anthropic: Refresh token expired", "auth"),
        ("server_error: API Error: 529 Overloaded.", "api-error"),
        ('400: {"message":"This model\'s maximum context length is 131072 tokens."}',
         "context-overflow"),
        ("Request timed out.", "network"),
        ("Provider finish_reason: content_filter", "api-error"),
        ("", ""),
    ]

    def test_cases(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(expected=expected, text=text[:40]):
                self.assertEqual(taxonomy.classify_api_error(text), expected)

    def test_api_classes_are_registered(self) -> None:
        for name in ("context-overflow", "auth", "network"):
            self.assertIsNotNone(taxonomy.lookup(name))
```

In `tests/test_classify.py`, `TaxonomyTest.CASES`, change `"input-validation"` to
`"invalid-args"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_taxonomy tests.test_classify -v`
Expected: FAIL. The new strings classify as `other`, `classify_api_error` does not exist, and
`invalid-args` is unknown.

- [ ] **Step 3: Replace `ERROR_CLASSES` in `sessionkit/taxonomy.py`**

```python
#: Ordered: the first class whose pattern matches wins, so specific classes precede generic
#: ones (a denied mkdir arrives as "Exit code 1 ... Permission denied"; a pi bash failure ends
#: with "Command exited with code N" after its real cause).
ERROR_CLASSES: tuple[FailureClass, ...] = (
    _error("hook-block", "environment", "harness",
           "Promote the rule into CLAUDE.md (AGENTS.md for pi) so the command is never "
           "emitted.",
           r"PreToolUse:|PostToolUse:.*hook error|\bBLOCKED:"),
    _error("user-rejected", "environment", "none",
           "Approach problem — the model proposed something the user declined.",
           r"user doesn't want to (?:proceed|take this action)|tool use was rejected"),
    # Distinct from user-rejected: no human saw this one, the harness blocked it.
    _error("policy-denied", "environment", "harness",
           "Blocked by permission policy — needs approval or a different approach.",
           r"denied by the Claude Code|auto mode classifier|"
           r"permission for this action was denied|"
           r"permission to use \w+.{0,200}?has been denied|"
           r"File is in a directory that is denied by your permission|"
           r"This session is isolated in the worktree|Tool execution was blocked",
           flags=re.I | re.S),
    _error("tool-error-structured", "environment", "env",
           'A tool returned a structured failure ({"ok": false}) — read its errors list; the '
           "input usually broke the tool's own rules.",
           r'^\s*\{\s*"ok"\s*:\s*false'),
    # Before invalid-args: pi silently repairs unparseable tool-call JSON to {}, so a
    # validation error on empty arguments means the serving stack lost them.
    _error("args-dropped", "tool-protocol", "serving",
           "The tool call reached the harness with empty arguments — the serving stack's "
           "tool-call parser lost them. Check the vLLM tool parser and chat template.",
           r"Received arguments:\s*\{\s*\}"),
    _error("invalid-args", "tool-protocol", "model",
           "Malformed tool call — the model passed arguments that fail the tool's schema.",
           r"InputValidationError|parameter `\w+` type is expected|"
           r'Validation failed for tool "|Invalid arguments|is required when'),
    _error("unknown-tool", "tool-protocol", "model",
           "The model called a tool that does not exist — often a sibling tool's name or one "
           "from another harness. Check the tool list it was given.",
           r"\bTool [\w.-]+ not found|No such tool available|Unknown skill:"),
    _error("truncated-call", "tool-protocol", "harness",
           "A tool call was cut off by the output token limit and never ran. Raise the model's "
           "maxTokens, or have it write in smaller pieces.",
           r"was not executed: the response hit the output token limit"),
    _error("edit-mismatch", "tool-protocol", "model",
           "The edit's old text did not match the file (whitespace, a stale view, or not "
           "unique). Re-read the exact region before editing.",
           r"String to replace not found|Found \d+ matches of the string|"
           r"Could not find the exact text in|Could not find edits\[|"
           r"Found \d+ occurrences of the text|No changes made to|"
           r"edits\[\d+\] and edits\[\d+\] overlap"),
    _error("tool-misuse", "tool-protocol", "model",
           "A tool was used on the wrong kind of target (a directory for a file read, an "
           "offset past the end). Check the path or range first.",
           r"\bEISDIR\b|Offset \d+ is beyond end of file"),
    _error("sibling-cascade", "tool-protocol", "none",
           "A parallel call failed only because a sibling in the same batch failed — look at "
           "the sibling's error instead.",
           r"Sibling tool call errored"),
    _error("aborted", "lifecycle", "none",
           "The user stopped this call or turn.",
           r"\[Request interrupted by user|Operation aborted|Command aborted"),
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
           r"error in mcp__|mcp server .* (failed|error)|No active transport"),
    _error("timeout", "generic", "none",
           "Raise the timeout or split the work.",
           r"timed out|timeout exceeded"),
    _error("exit-code", "generic", "none",
           "Genuine command failure — read the command output.",
           r"^exit code \d+", r"Command exited with code \d+\s*$"),
)
```

- [ ] **Step 4: Add pi's exit-code suffix and the API-error classes**

Replace the `_EXIT_RE = ...` line with:

```python
_EXIT_RE = re.compile(r"^exit code (\d+)\b", re.I)
#: pi appends the status to the *end* of bash output: "...\n\nCommand exited with code N".
_PI_EXIT_RE = re.compile(r"Command exited with code (\d+)\s*$", re.I)
```

After the `KILLED = ...` definition, add:

```python
_OVERFLOW = _error(
    "context-overflow", "environment", "harness",
    "The prompt plus the reserved output tokens exceeded the context window. Lower the "
    "model's maxTokens reservation (32K reserved on a 131K window leaves 98K for the "
    "conversation) or compact earlier; splitting the work is the fallback.",
    r"maximum context length|context length exceeded|context window|prompt is too long|"
    r"Context overflow recovery failed")
_AUTH = _error(
    "auth", "environment", "env",
    "The provider sign-in is missing or expired — re-authenticate. Not a model or prompt "
    "problem.",
    r"authentication_failed|invalid_grant|oauth|\b401\b|\b403\b|please run /login|"
    r"not logged in|login expired|unauthori[sz]ed")
_NETWORK = _error(
    "network", "environment", "env",
    "The model endpoint was unreachable or too slow to answer — check the server's health "
    "and load, and whether something else was using it at the time.",
    r"ECONNREFUSED|ECONNRESET|ENOTFOUND|getaddrinfo|fetch failed|socket hang up|timed out|"
    r"timeout|network error")
API_ERROR_CLASSES: tuple[FailureClass, ...] = (_OVERFLOW, _AUTH, _NETWORK)

#: Ordered API-error rules. ``rate-limit`` reuses the tool-result class name with patterns
#: that match provider wording ("rate_limit", "hit your session limit").
_API_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (_OVERFLOW.name, _OVERFLOW.patterns),
    (_AUTH.name, _AUTH.patterns),
    ("rate-limit", (re.compile(
        r"rate.?limit|hit your (?:session )?limit|\b429\b|too many requests|quota", re.I),)),
    (_NETWORK.name, _NETWORK.patterns),
)
```

Change `ALL_CLASSES` to:

```python
ALL_CLASSES: tuple[FailureClass, ...] = (*ERROR_CLASSES, KILLED, *API_ERROR_CLASSES,
                                         *FINDING_KINDS)
```

In `classify_error`, replace everything from `match = _EXIT_RE.match(body)` through
`return known` with:

```python
    code = _exit_code(body)
    if code is not None and code in _EXIT_CODES:
        return _EXIT_CODES[code]
```

and add these two functions after `classify_error`:

```python
def _exit_code(body: str) -> int | None:
    """The exit code in Claude Code's ``Exit code N`` prefix or pi's trailing suffix."""
    match = _EXIT_RE.match(body) or _PI_EXIT_RE.search(body)
    return int(match.group(1)) if match else None


def classify_api_error(text: str) -> str:
    """Classify a failed model call's error text (``Message.error_text``).

    Returns:
        ``context-overflow``, ``auth``, ``rate-limit``, ``network``, the catch-all
        ``api-error``, or ``""`` for empty text.
    """
    body = (text or "").strip()
    if not body:
        return ""
    for name, patterns in _API_RULES:
        if any(p.search(body) for p in patterns):
            return name
    return "api-error"
```

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sessionkit/taxonomy.py tests/test_taxonomy.py tests/test_classify.py
git commit -m "Classify pi and Claude Code tool-call and API failures by name"
```

---

### Task 8: Tool-protocol and generation detectors

**Files:**
- Create: `sessionkit/detectors/tool_protocol.py` and `sessionkit/detectors/generation.py`.
- Create: `tests/detector_fixtures.py`, which the detector tests in Tasks 8–13 share.
- Modify: `sessionkit/taxonomy.py` (the `FINDING_KINDS` and `THRESHOLDS` entries).
- Modify: `sessionkit/detectors/__init__.py` (the registry).
- Test: `tests/test_detectors_protocol.py` (new).

**Interfaces:**
- Consumes:
  - `taxonomy.lookup` and `classify_api_error` (Task 7);
  - the `Message` fields (Tasks 4 and 5);
  - `features.parse_ts` (Task 3).
- Produces:
  - `tool_protocol.error_classes` and `tool_protocol.tool_text_leak`;
  - `generation.empty_turn`, `degeneration`, `output_truncated` and `generation_stall`;
  - `generation.assistant_latencies(session) -> list[tuple[Message, int]]`, which Task 14
    uses;
  - the finding kinds `tool-text-leak`, `empty-turn`, `degeneration`, `output-truncated` and
    `generation-stall`.
- Error classes in the tool-protocol, generation and environment families also become
  findings, named after the class, unless their fix target is `none`.

- [ ] **Step 1: Create `tests/detector_fixtures.py`**

```python
"""Tiny builders for detector unit tests: a ParsedSession assembled field by field."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sessionkit.parse import FileOp, Message, ParsedSession, SysEvent, ToolCall

_BASE = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)

__all__ = ["FileOp", "SysEvent", "call", "msg", "session", "ts"]


def ts(minutes: float) -> str:
    """An ISO timestamp ``minutes`` after a fixed base time."""
    stamp = (_BASE + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def session(harness: str = "pi", sid: str = "s", **kw: Any) -> ParsedSession:
    return ParsedSession(sid=sid, source_id="src", path=f"/tmp/{sid}.jsonl", harness=harness,
                         **kw)


def call(line: int, name: str = "bash", args: dict[str, Any] | None = None, *,
         is_error: bool = False, err_class: str = "", at: str = "", result_at: str = "",
         dur_ms: int | None = None, digest: str = "", result_line: int = 0, out: str = "",
         tid: str = "") -> ToolCall:
    return ToolCall(tool_use_id=tid or f"t{line}-{name}", line=line, name=name, ts=at,
                    result_ts=result_at, result_line=result_line or line, dur_ms=dur_ms,
                    is_error=is_error, err_class=err_class, input_digest=digest or f"d{line}",
                    input_preview=json.dumps(args or {}), output_preview=out)


def msg(line: int, role: str = "assistant", *, text: str = "", at: str = "", stop: str = "",
        tool_calls: int = 0, reasoning_len: int = 0, error_text: str = "",
        markup_leak: str = "", repeat_ratio: float = 0.0, refs: list[str] | None = None,
        claims_done: bool = False) -> Message:
    return Message(uuid=f"m{line}", line=line, ts=at, role=role, text_len=len(text),
                   preview=text, stop_reason=stop, tool_calls=tool_calls,
                   reasoning_len=reasoning_len, error_text=error_text,
                   markup_leak=markup_leak, repeat_ratio=repeat_ratio,
                   refs=list(refs or []), claims_done=claims_done)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_detectors_protocol.py`:

```python
"""Tool-protocol and generation detectors."""

from __future__ import annotations

import unittest

from sessionkit.detectors import detect, generation, tool_protocol
from tests import detector_fixtures as df


def _kinds(session) -> list[str]:
    return [a.kind for a in detect(session)]


class ErrorClassFindingTest(unittest.TestCase):
    def test_surfaced_classes_become_findings(self) -> None:
        s = df.session(tools=[
            df.call(1, "edit", is_error=True, err_class="edit-mismatch"),
            df.call(2, is_error=True, err_class="exit-code"),
            df.call(3, is_error=True, err_class="aborted"),
        ], messages=[df.msg(4, stop="error", error_text="Request timed out.")])
        kinds = {a.kind: a for a in tool_protocol.error_classes(s)}
        self.assertEqual(set(kinds), {"edit-mismatch", "network"})
        self.assertEqual(kinds["network"].lines, [4])

    def test_clean_session_has_none(self) -> None:
        self.assertEqual(tool_protocol.error_classes(df.session(tools=[df.call(1)])), [])


class ToolTextLeakTest(unittest.TestCase):
    def test_leak_without_a_real_call(self) -> None:
        s = df.session(messages=[df.msg(2, markup_leak="json-call", stop="end")])
        self.assertIn("tool-text-leak", _kinds(s))

    def test_markup_alongside_a_real_call_is_not_a_leak(self) -> None:
        s = df.session(messages=[df.msg(2, markup_leak="json-call", tool_calls=1)])
        self.assertNotIn("tool-text-leak", _kinds(s))


class GenerationTest(unittest.TestCase):
    def test_empty_turn(self) -> None:
        s = df.session(messages=[df.msg(2, stop="end", reasoning_len=500)])
        self.assertIn("empty-turn", _kinds(s))

    def test_claude_code_intermediate_records_are_not_empty_turns(self) -> None:
        s = df.session("claude-code", messages=[df.msg(2, stop="", reasoning_len=300),
                                                df.msg(3, stop="tool_use", tool_calls=1)])
        self.assertNotIn("empty-turn", _kinds(s))

    def test_degeneration_includes_aborted_turns(self) -> None:
        s = df.session(messages=[df.msg(60, stop="aborted", repeat_ratio=0.85)])
        self.assertIn("degeneration", _kinds(s))

    def test_varied_text_is_not_degeneration(self) -> None:
        s = df.session(messages=[df.msg(60, stop="end", text="x", repeat_ratio=0.2)])
        self.assertNotIn("degeneration", _kinds(s))

    def test_output_truncated(self) -> None:
        s = df.session(messages=[df.msg(5, stop="length", text="partial")])
        self.assertIn("output-truncated", _kinds(s))


class GenerationStallTest(unittest.TestCase):
    def _slow_session(self, final_gap: float, step: float = 2.5):
        messages = [df.msg(1, "user", text="go", at=df.ts(0))]
        tools = []
        clock, line = 0.0, 2
        for _ in range(4):
            clock += step
            messages.append(df.msg(line, text="step", at=df.ts(clock), stop="tool_use",
                                   tool_calls=1))
            tools.append(df.call(line, at=df.ts(clock), result_at=df.ts(clock + 0.1)))
            clock += 0.1
            line += 2
        messages.append(df.msg(line, at=df.ts(clock + final_gap), stop="aborted"))
        return df.session(messages=messages, tools=tools), line

    def test_an_outlier_turn_fires(self) -> None:
        s, line = self._slow_session(final_gap=18)
        stalls = [a for a in detect(s) if a.kind == "generation-stall"]
        self.assertEqual([a.lines for a in stalls], [[line]])

    def test_a_uniformly_slow_model_does_not_fire(self) -> None:
        s, _line = self._slow_session(final_gap=12, step=12)
        self.assertNotIn("generation-stall", _kinds(s))

    def test_unusable_timestamps_are_skipped(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go", at=""),
                                 df.msg(2, at="garbage"), df.msg(3, at="")])
        self.assertEqual(generation.assistant_latencies(s), [])
        self.assertNotIn("generation-stall", _kinds(s))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_detectors_protocol -v`
Expected: FAIL with `ImportError: cannot import name 'generation'`.

- [ ] **Step 4: Add the thresholds and finding kinds**

In `sessionkit/taxonomy.py`, add to `THRESHOLDS`:

```python
    "degeneration_ratio": 0.5,          # repeated word 8-grams within one message
    "generation_stall_ms": 600_000,     # a model turn slower than this ...
    "generation_stall_factor": 5,       # ... and this many times the session's median
```

Append to `FINDING_KINDS` (before its closing parenthesis):

```python
    _kind("tool-text-leak", "tool-protocol", TIER_S, "serving",
          "The model wrote a tool call as plain text, so nothing ran and the turn ended. "
          "Check the serving stack's tool-call parser and chat template for this model "
          "(vLLM --tool-call-parser); long contexts make it worse."),
    _kind("empty-turn", "generation", TIER_S, "serving",
          "The model ended its turn with no text and no tool call — usually reasoning-only "
          "output. Check the chat template and reasoning parser, or lower the thinking level."),
    _kind("degeneration", "generation", TIER_S, "serving",
          "The model repeated itself within one turn — a sampling degeneration loop. Raise "
          "temperature or add a repetition penalty for this model, lower the thinking level, "
          "or keep the context smaller before hard steps."),
    _kind("output-truncated", "generation", TIER_S, "harness",
          "The response hit the output token limit mid-turn. Raise the model's maxTokens, or "
          "ask for smaller steps (write files in parts)."),
    _kind("generation-stall", "generation", TIER_S, "serving",
          "One model turn took far longer than the session's norm — a runaway reasoning loop "
          "or an overloaded server. Check that turn's reasoning for repetition and the "
          "server's load at the time."),
```

- [ ] **Step 5: Create `sessionkit/detectors/tool_protocol.py`**

```python
"""Tool-protocol detectors: tool calls that failed as calls, not as the work they did."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import classify_api_error, lookup

#: Error classes in these families also surface as session findings (spec §2).
SURFACED_FAMILIES = ("tool-protocol", "generation", "environment")


def _surfaced(name: str) -> bool:
    """Surface a class only if it names something fixable (fix target is not ``none``)."""
    entry = lookup(name)
    return entry is not None and entry.family in SURFACED_FAMILIES and \
        entry.fix_target != "none"


def error_classes(session: ParsedSession) -> list[Anomaly]:
    """One finding per surfaced error class present: failed tool calls and failed model
    calls (an assistant message whose ``stop_reason`` is ``error``)."""
    lines: dict[str, list[int]] = {}
    for call in session.tools:
        if call.is_error and _surfaced(call.err_class):
            lines.setdefault(call.err_class, []).append(call.line)
    for message in session.messages:
        if message.stop_reason == "error" and message.error_text:
            kind = classify_api_error(message.error_text)
            if _surfaced(kind):
                lines.setdefault(kind, []).append(message.line)
    return [Anomaly(kind, f"{len(found)} occurrence(s)", len(found), found[:MAX_LINES])
            for kind, found in lines.items()]


def tool_text_leak(session: ParsedSession) -> list[Anomaly]:
    """Assistant text carrying tool-call markup while no structured call was made."""
    hits = [m for m in session.messages
            if m.role == "assistant" and m.markup_leak and m.tool_calls == 0]
    if not hits:
        return []
    formats = ", ".join(sorted({m.markup_leak for m in hits}))
    return [Anomaly("tool-text-leak", f"tool-call markup in plain text ({formats})", len(hits),
                    [m.line for m in hits][:MAX_LINES])]
```

- [ ] **Step 6: Create `sessionkit/detectors/generation.py`**

```python
"""Generation detectors: what went wrong inside the model's own output."""

from __future__ import annotations

import bisect
import statistics

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.features import parse_ts
from sessionkit.parse import Message, ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def empty_turn(session: ParsedSession) -> list[Anomaly]:
    """A turn that ended normally with no visible text and no tool call.

    Requires ``stop_reason == "end"``: Claude Code's intermediate per-block records carry no
    stop reason and legitimately hold only reasoning.
    """
    hits = [m.line for m in session.messages if m.role == "assistant"
            and m.stop_reason == "end" and m.tool_calls == 0 and m.text_len == 0]
    if not hits:
        return []
    return [Anomaly("empty-turn", "turn ended with no text and no tool call", len(hits),
                    hits[:MAX_LINES])]


def degeneration(session: ParsedSession) -> list[Anomaly]:
    """A message (visible or reasoning text, any stop reason) that repeats itself."""
    limit = THRESHOLDS["degeneration_ratio"]
    hits = [m for m in session.messages if m.role == "assistant" and m.repeat_ratio >= limit]
    if not hits:
        return []
    worst = max(m.repeat_ratio for m in hits)
    return [Anomaly("degeneration", f"repeated text within one turn (ratio {worst:.2f})",
                    len(hits), [m.line for m in hits][:MAX_LINES])]


def output_truncated(session: ParsedSession) -> list[Anomaly]:
    """Turns cut off by the output token limit."""
    hits = [m.line for m in session.messages
            if m.role == "assistant" and m.stop_reason == "length"]
    if not hits:
        return []
    return [Anomaly("output-truncated", "turn hit the output token limit", len(hits),
                    hits[:MAX_LINES])]


def assistant_latencies(session: ParsedSession) -> list[tuple[Message, int]]:
    """Each assistant message with the milliseconds since the entry before it.

    Messages and events with unusable timestamps are skipped rather than guessed at.
    """
    stamps = [m.ts for m in session.messages]
    stamps += [c.result_ts for c in session.tools]
    stamps += [e.ts for e in session.sysev]
    events = sorted(t for t in (parse_ts(s) for s in stamps) if t is not None)
    out: list[tuple[Message, int]] = []
    for message in session.messages:
        at = parse_ts(message.ts) if message.role == "assistant" else None
        if at is None:
            continue
        index = bisect.bisect_left(events, at)
        if index > 0:
            out.append((message, int((at - events[index - 1]).total_seconds() * 1000)))
    return out


def generation_stall(session: ParsedSession) -> list[Anomaly]:
    """A model turn that took far longer than both an absolute limit and the session's norm."""
    latencies = assistant_latencies(session)
    if not latencies:
        return []
    median = statistics.median(ms for _m, ms in latencies) if len(latencies) >= 3 else 0
    limit = THRESHOLDS["generation_stall_ms"]
    factor = THRESHOLDS["generation_stall_factor"]
    return [Anomaly("generation-stall",
                    f"model turn took {ms // 60_000} min (session median {int(median) // 1000}s)",
                    1, [message.line])
            for message, ms in latencies if ms > limit and ms > factor * median]
```

- [ ] **Step 7: Register them**

In `sessionkit/detectors/__init__.py`, change the import to
`from sessionkit.detectors import generation, lifecycle, loops, tool_protocol`, then append to
`DETECTORS`:

```python
    tool_protocol.error_classes,
    tool_protocol.tool_text_leak,
    generation.empty_turn,
    generation.degeneration,
    generation.output_truncated,
    generation.generation_stall,
```

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/detectors sessionkit/taxonomy.py tests/detector_fixtures.py \
  tests/test_detectors_protocol.py
git commit -m "Detect tool-call leaks, empty turns, degeneration and generation stalls"
```

---

### Task 9: Loop detectors (new and retuned)

**Files:**
- Modify: `sessionkit/detectors/loops.py`.
- Modify: `sessionkit/taxonomy.py` (`THRESHOLDS` and `FINDING_KINDS`).
- Modify: `tests/test_classify.py`. These are **intended changes**:
  - `test_file_thrash` now builds 8 edits and expects lines `[1..8]`;
  - `test_read_loop` now builds 5 reads and expects lines `[1..5]`.
- Test: `tests/test_detectors_loops.py` (new).

**Interfaces:**
- Consumes:
  - `harness.role` and `call_paths` (Task 2);
  - the `edit-mismatch` and `sibling-cascade` classes (Task 7).
- Produces:
  - the finding kinds `edit-mismatch-spiral` and `read-only-spiral`.
- Changes to existing detectors:
  - `repeat-tool` exempts calls with role `poll`;
  - `file-thrash` fires at 8 or more edits, or at 5 or more edits with at least 2
    failures;
  - `read-loop` fires at 5 or more counted reads. A re-read after a compaction, or after an
    edit of that path, is not counted;
  - `error-cascade` skips `sibling-cascade` calls.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detectors_loops.py`:

```python
"""Loop detectors: new kinds and the retuned thresholds."""

from __future__ import annotations

import unittest

from sessionkit.detectors import detect
from tests import detector_fixtures as df


def _kinds(session) -> list[str]:
    return [a.kind for a in detect(session)]


class RepeatToolTest(unittest.TestCase):
    def test_polling_is_not_a_repeat_loop(self) -> None:
        s = df.session(tools=[df.call(i, "bg_wait", digest="same") for i in range(1, 6)])
        self.assertNotIn("repeat-tool", _kinds(s))

    def test_identical_commands_still_are(self) -> None:
        s = df.session(tools=[df.call(i, "bash", digest="same") for i in range(1, 4)])
        self.assertIn("repeat-tool", _kinds(s))


class EditMismatchSpiralTest(unittest.TestCase):
    def test_three_failures_on_one_file(self) -> None:
        s = df.session(tools=[df.call(i, "edit", {"path": "/a.py"}, is_error=True,
                                      err_class="edit-mismatch") for i in (2, 4, 6)])
        spiral = [a for a in detect(s) if a.kind == "edit-mismatch-spiral"]
        self.assertEqual([(a.detail, a.lines) for a in spiral], [("/a.py", [2, 4, 6])])

    def test_failures_spread_over_files_are_not(self) -> None:
        s = df.session(tools=[df.call(i, "edit", {"path": f"/{i}.py"}, is_error=True,
                                      err_class="edit-mismatch") for i in (2, 4, 6)])
        self.assertNotIn("edit-mismatch-spiral", _kinds(s))


class ReadOnlySpiralTest(unittest.TestCase):
    def test_long_run_of_reads_and_searches(self) -> None:
        tools = [df.call(i, "read" if i % 2 else "grep", {"path": f"/{i}.py"})
                 for i in range(1, 9)]
        self.assertIn("read-only-spiral", _kinds(df.session(tools=tools)))

    def test_an_edit_breaks_the_run(self) -> None:
        tools = [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(1, 5)]
        tools.append(df.call(5, "edit", {"path": "/1.py"}))
        tools += [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(6, 10)]
        self.assertNotIn("read-only-spiral", _kinds(df.session(tools=tools)))

    def test_a_substantial_answer_breaks_the_run(self) -> None:
        tools = [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(1, 5)]
        tools += [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(6, 10)]
        messages = [df.msg(5, text="x" * 250, stop="tool_use", tool_calls=1)]
        self.assertNotIn("read-only-spiral",
                         _kinds(df.session(tools=tools, messages=messages)))


class FileThrashTest(unittest.TestCase):
    def test_five_edits_with_two_failures(self) -> None:
        tools = [df.call(i, "edit", is_error=i in (2, 3), tid=f"e{i}") for i in range(1, 6)]
        files = [df.FileOp("/a.py", "edit", "", f"e{i}", line=i) for i in range(1, 6)]
        self.assertIn("file-thrash", _kinds(df.session(tools=tools, files=files)))

    def test_seven_clean_edits_are_iterative_work(self) -> None:
        tools = [df.call(i, "edit", tid=f"e{i}") for i in range(1, 8)]
        files = [df.FileOp("/a.py", "edit", "", f"e{i}", line=i) for i in range(1, 8)]
        self.assertNotIn("file-thrash", _kinds(df.session(tools=tools, files=files)))


class ReadLoopTest(unittest.TestCase):
    def test_rereads_after_edits_and_compaction_do_not_count(self) -> None:
        files = [df.FileOp("/a.py", "read", "", "t", line=1),
                 df.FileOp("/a.py", "edit", "", "t", line=2),
                 df.FileOp("/a.py", "read", "", "t", line=3),
                 df.FileOp("/a.py", "read", "", "t", line=5),
                 df.FileOp("/a.py", "read", "", "t", line=6),
                 df.FileOp("/a.py", "read", "", "t", line=7)]
        sysev = [df.SysEvent(4, "", "compaction")]
        # Counted reads: 1, 6, 7 (3 is after an edit, 5 is after a compaction) → below 5.
        self.assertNotIn("read-loop", _kinds(df.session(files=files, sysev=sysev)))


class ErrorCascadeTest(unittest.TestCase):
    def test_sibling_cascades_do_not_count(self) -> None:
        tools = [df.call(1, is_error=True, err_class="exit-code"),
                 df.call(2, is_error=True, err_class="sibling-cascade"),
                 df.call(3, is_error=True, err_class="sibling-cascade"),
                 df.call(4)]
        self.assertNotIn("error-cascade", _kinds(df.session(tools=tools)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

In `tests/test_classify.py`, replace `test_file_thrash` and `test_read_loop` with:

```python
    def test_file_thrash(self) -> None:
        session = _session()
        session.files.extend(FileOp("/a.py", "edit", "", "t", line=i) for i in range(1, 9))
        anomaly = next(a for a in detect(session) if a.kind == "file-thrash")
        self.assertEqual(anomaly.lines, [1, 2, 3, 4, 5, 6, 7, 8])

    def test_read_loop(self) -> None:
        session = _session()
        session.files.extend(FileOp("/a.py", "read", "", "t", line=i) for i in range(1, 6))
        anomaly = next(a for a in detect(session) if a.kind == "read-loop")
        self.assertEqual(anomaly.lines, [1, 2, 3, 4, 5])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_detectors_loops -v`
Expected: FAIL. Polling still counts as a repeat, the new kinds are missing, and 7 clean
edits still trip `file-thrash`.

- [ ] **Step 3: Thresholds and finding kinds**

In `sessionkit/taxonomy.py`, `THRESHOLDS`, change `"file_thrash": 5` to `"file_thrash": 8`
and `"read_loop": 4` to `"read_loop": 5`, then add:

```python
    "file_thrash_min": 5,         # ... or this many edits to one path
    "file_thrash_failures": 2,    # ... when at least this many of them failed
    "edit_mismatch_spiral": 3,    # edit-mismatch failures on one path
    "read_only_spiral": 8,        # consecutive read/search calls
    "read_only_text_chars": 200,  # an assistant answer at least this long breaks the run
```

Append to `FINDING_KINDS`:

```python
    _kind("edit-mismatch-spiral", "loop", TIER_S, "model",
          "Edits to the same file failed repeatedly — the model is editing from a stale or "
          "imagined view of the file. Re-read the exact region before each edit."),
    _kind("read-only-spiral", "loop", TIER_S, "model",
          "A long run of reads and searches with no edit, command or answer — the model is "
          "gathering instead of acting. Ask for a plan or a first change."),
```

- [ ] **Step 4: Update `sessionkit/detectors/loops.py`**

Add the import `from sessionkit.harness import call_paths, role`. Replace `repeat_tool`,
`_path_churn`, `file_thrash`, `read_loop` and `error_cascade` with the code below, which also
adds the two new detectors. Delete the now-unused `_path_churn`.

```python
def repeat_tool(session: ParsedSession) -> list[Anomaly]:
    """Identical (tool, input) invoked repeatedly — the loop signal. Polling tools exist to
    be called repeatedly, so role ``poll`` is exempt."""
    seen: dict[tuple[str, str], list[int]] = {}
    for call in session.tools:
        if role(session.harness, call.name) == "poll":
            continue
        seen.setdefault((call.name, call.input_digest), []).append(call.line)
    out = []
    for (name, _digest), lines in seen.items():
        if len(lines) >= THRESHOLDS["repeat_tool"]:
            out.append(Anomaly("repeat-tool", f"{name} called with identical input",
                               len(lines), lines[:MAX_LINES]))
    return out


def file_thrash(session: ParsedSession) -> list[Anomaly]:
    """The same path edited many times, or several times with failures in between.

    Clean iterative edits are normal work: at the old threshold of 5, only 14 of 145 corpus
    hits involved a real edit failure.
    """
    failed = {c.tool_use_id for c in session.tools if c.is_error}
    edits: dict[str, list[int]] = {}
    failures: dict[str, int] = {}
    for f in session.files:
        if f.op != "edit":
            continue
        edits.setdefault(f.path, []).append(f.line)
        if f.tool_use_id in failed:
            failures[f.path] = failures.get(f.path, 0) + 1
    out = []
    for path, lines in edits.items():
        many = len(lines) >= THRESHOLDS["file_thrash"]
        failing = len(lines) >= THRESHOLDS["file_thrash_min"] and \
            failures.get(path, 0) >= THRESHOLDS["file_thrash_failures"]
        if many or failing:
            out.append(Anomaly("file-thrash", path, len(lines), lines))
    return out


def read_loop(session: ParsedSession) -> list[Anomaly]:
    """The same path read repeatedly. A re-read after that path was edited, or after a
    compaction erased the model's copy, is a legitimate refresh and is not counted."""
    events = sorted([(f.line, 1, f) for f in session.files]
                    + [(e.line, 0, e) for e in session.sysev if e.subtype == "compaction"],
                    key=lambda e: (e[0], e[1]))
    counted: dict[str, list[int]] = {}
    edited: set[str] = set()
    read_at_compaction: dict[str, int] = {}
    compactions = 0
    for line, kind, item in events:
        if kind == 0:
            compactions += 1
            continue
        if item.op == "edit":
            edited.add(item.path)
            continue
        if item.op != "read":
            continue
        refresh = item.path in edited or \
            read_at_compaction.get(item.path, compactions) < compactions
        edited.discard(item.path)
        read_at_compaction[item.path] = compactions
        if not refresh:
            counted.setdefault(item.path, []).append(line)
    limit = THRESHOLDS["read_loop"]
    return [Anomaly("read-loop", path, len(ls), ls)
            for path, ls in counted.items() if len(ls) >= limit]


def error_cascade(session: ParsedSession) -> list[Anomaly]:
    """Runs of consecutive failing tool calls. A sibling-cascade failure only echoes another
    call's error, so it neither extends nor breaks a run."""
    out: list[Anomaly] = []
    run, lines = 0, []
    for call in session.tools:
        if call.err_class == "sibling-cascade":
            continue
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


def edit_mismatch_spiral(session: ParsedSession) -> list[Anomaly]:
    """Edit-mismatch failures piling up on one file."""
    by_path: dict[str, list[int]] = {}
    for call in session.tools:
        if call.err_class == "edit-mismatch":
            paths = call_paths(session.harness, call)
            by_path.setdefault(paths[0] if paths else call.name, []).append(call.line)
    limit = THRESHOLDS["edit_mismatch_spiral"]
    return [Anomaly("edit-mismatch-spiral", path, len(ls), ls[:MAX_LINES])
            for path, ls in by_path.items() if len(ls) >= limit]


_READ_ONLY = ("read", "search")
_PROGRESS = ("edit", "write", "exec")


def read_only_spiral(session: ParsedSession) -> list[Anomaly]:
    """A long run of read/search calls with no edit, command, or substantial answer."""
    events = sorted([(m.line, 0, m) for m in session.messages if m.role == "assistant"]
                    + [(c.line, 1, c) for c in session.tools], key=lambda e: (e[0], e[1]))
    out: list[Anomaly] = []
    run: list[int] = []
    for _line, kind, item in events:
        if kind == 0:
            if item.text_len >= THRESHOLDS["read_only_text_chars"]:
                out.extend(_spiral(run))
                run = []
            continue
        call_role = role(session.harness, item.name)
        if call_role in _READ_ONLY:
            run.append(item.line)
        elif call_role in _PROGRESS:
            out.extend(_spiral(run))
            run = []
    out.extend(_spiral(run))
    return out


def _spiral(run: list[int]) -> list[Anomaly]:
    if len(run) < THRESHOLDS["read_only_spiral"]:
        return []
    return [Anomaly("read-only-spiral",
                    f"{len(run)} consecutive read/search calls with no edit, command or answer",
                    len(run), run[:MAX_LINES])]
```

- [ ] **Step 5: Register the new detectors**

Append to `DETECTORS` in `sessionkit/detectors/__init__.py`:

```python
    loops.edit_mismatch_spiral,
    loops.read_only_spiral,
```

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sessionkit/detectors sessionkit/taxonomy.py tests/test_detectors_loops.py \
  tests/test_classify.py
git commit -m "Add edit-mismatch and read-only spirals; retune thrash and read-loop"
```

---

### Task 10: Lifecycle detectors, end states, and liveness plumbing

**Files:**
- Modify: `sessionkit/detectors/lifecycle.py`. Rewrite `stalls`; add `hung`,
  `abrupt_truncation`, `user_interrupt` and `interrupt_points`.
- Modify: `sessionkit/detectors/__init__.py` (`detect(session, live=False)`).
- Modify: `sessionkit/classify.py` (`derive_end_state`).
- Modify: `sessionkit/corpus.py`. `load_one`, `_load_pi`, `load` and `load_session` pass
  liveness through.
- Modify: `sessionkit/taxonomy.py` (`THRESHOLDS` and `FINDING_KINDS`).
- Modify: `tests/test_classify.py`. `test_stall` now uses `name="Read"`, because a 200 s
  `Bash` call with no declared timeout no longer stalls. This is an **intended change**.
- Test: `tests/test_detectors_lifecycle.py` (new), plus three new end-state tests in
  `tests/test_classify.py`.

**Interfaces:**
- Consumes:
  - `harness.role`, `declared_timeout_ms` and `is_interrupt_marker` (Task 2);
  - the `aborted` error class (Task 7).
- Produces:
  - `detectors.detect(session, live: bool = False)`;
  - `lifecycle.interrupt_points(session) -> list[int]` and
    `lifecycle.user_interrupt(session) -> list[Anomaly]`, which Task 12 uses;
  - the end states `aborted`, `output-truncated` and `agent-unresponsive`;
  - `corpus.load_one(source, path, dir_name, live_sids=frozenset())`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detectors_lifecycle.py`:

```python
"""Lifecycle detectors: stalls, hangs, truncated transcripts, user interrupts."""

from __future__ import annotations

import unittest

from sessionkit.detectors import detect, lifecycle
from tests import detector_fixtures as df


def _kinds(session, live: bool = False) -> list[str]:
    return [a.kind for a in detect(session, live)]


class StallTest(unittest.TestCase):
    CASES = [
        ("pi", "read", {}, 200_000, True),
        ("pi", "bash", {"command": "x"}, 300_000, False),
        ("pi", "bash", {"command": "x"}, 700_000, True),
        ("pi", "bash", {"command": "x", "timeout": 30}, 28_000, True),
        ("pi", "bash", {"command": "x", "timeout": 30}, 10_000, False),
        ("claude-code", "Bash", {"command": "x", "timeout": 600_000}, 400_000, False),
        ("pi", "edit", {"path": "/a"}, 900_000, False),
        ("claude-code", "AskUserQuestion", {}, 900_000, False),
        ("pi", "subagent", {}, 3_600_000, False),
    ]

    def test_cases(self) -> None:
        for harness, name, args, dur, expected in self.CASES:
            with self.subTest(harness=harness, name=name, dur=dur):
                s = df.session(harness, tools=[df.call(1, name, args, dur_ms=dur)])
                self.assertEqual("stall" in _kinds(s), expected)


class HungTest(unittest.TestCase):
    def test_ended_waiting_on_a_command(self) -> None:
        s = df.session(tools=[df.call(3, err_class="no-result")], end_state="interrupted-tool")
        self.assertIn("hung", _kinds(s))

    def test_waiting_on_the_user_is_not_hung(self) -> None:
        s = df.session("claude-code", tools=[df.call(3, "AskUserQuestion", err_class="no-result")],
                       end_state="interrupted-tool")
        self.assertNotIn("hung", _kinds(s))

    def test_a_live_session_is_never_hung(self) -> None:
        s = df.session(tools=[df.call(3, err_class="no-result")], end_state="interrupted-tool")
        self.assertNotIn("hung", _kinds(s, live=True))

    def test_pi_turn_that_never_finished_streaming(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go"), df.msg(2, stop="")])
        self.assertIn("hung", _kinds(s))

    def test_claude_code_null_stop_records_are_not_hung(self) -> None:
        s = df.session("claude-code", messages=[df.msg(1, "user", text="go"),
                                                df.msg(2, text="ok", stop="")])
        self.assertNotIn("hung", _kinds(s))


class AbruptTruncationTest(unittest.TestCase):
    def test_partial_last_line(self) -> None:
        self.assertIn("abrupt-truncation", _kinds(df.session(last_line_malformed=True)))
        self.assertNotIn("abrupt-truncation",
                         _kinds(df.session(last_line_malformed=True), live=True))


class UserInterruptTest(unittest.TestCase):
    def test_pi_abort_then_redirect(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="set up profiles"),
                                 df.msg(33, stop="aborted"),
                                 df.msg(34, "user", text="just write a script")])
        found = lifecycle.user_interrupt(s)
        self.assertEqual([a.lines for a in found], [[33, 34]])

    def test_claude_code_marker_then_redirect(self) -> None:
        s = df.session("claude-code", messages=[
            df.msg(1, "user", text="go"),
            df.msg(5, "user", text="[Request interrupted by user]"),
            df.msg(6, "user", text="no, do it this way")])
        self.assertEqual([a.lines for a in lifecycle.user_interrupt(s)], [[5, 6]])

    def test_one_redirect_is_reported_once(self) -> None:
        s = df.session(tools=[df.call(32, is_error=True, err_class="aborted")],
                       messages=[df.msg(33, stop="aborted"), df.msg(34, "user", text="stop")])
        self.assertEqual(len(lifecycle.user_interrupt(s)), 1)

    def test_abort_without_a_redirect_is_not_an_interrupt_finding(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go"), df.msg(60, stop="aborted")])
        self.assertEqual(lifecycle.user_interrupt(s), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

In `tests/test_classify.py`, change `test_stall`'s call to `_call(1, name="Read",
dur=200_000)`, then add to `EndStateTest`:

```python
    def test_aborted_final_turn(self) -> None:
        from sessionkit.parse import Message
        session = _session()
        session.messages.append(Message(uuid="u", line=2, ts="", role="assistant",
                                        stop_reason="aborted"))
        self.assertEqual(derive_end_state(session)[0], "aborted")

    def test_output_truncated_final_turn(self) -> None:
        from sessionkit.parse import Message
        session = _session()
        session.messages.append(Message(uuid="u", line=2, ts="", role="assistant",
                                        stop_reason="length"))
        self.assertEqual(derive_end_state(session)[0], "output-truncated")

    def test_agent_unresponsive(self) -> None:
        from sessionkit.parse import Message
        session = _session()
        session.messages.extend([
            Message(uuid="a", line=83, ts="", role="assistant", stop_reason="aborted"),
            Message(uuid="u", line=84, ts="", role="user", text_len=5, preview="hello")])
        self.assertEqual(derive_end_state(session)[0], "agent-unresponsive")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_detectors_lifecycle tests.test_classify -v`
Expected: FAIL. `detect()` takes no `live` argument, `hung` and `user_interrupt` don't
exist, and the new end states come back as `complete` or `interrupted-user`.

- [ ] **Step 3: Thresholds and finding kinds**

In `sessionkit/taxonomy.py`, add to `THRESHOLDS`:

```python
    "stall_exec_ms": 600_000,        # a command with no declared timeout running this long
    "stall_timeout_fraction": 0.9,   # a command reaching this share of its declared timeout
```

Append to `FINDING_KINDS`:

```python
    _kind("hung", "lifecycle", TIER_S, "env",
          "The session ended waiting on a call or a model turn that never finished — check the "
          "tool or server that was pending."),
    _kind("abrupt-truncation", "lifecycle", TIER_S, "none",
          "The transcript ends in a partial write — the agent process died mid-record."),
    _kind("user-interrupt", "lifecycle", TIER_S, "none",
          "The user stopped the agent and redirected it — the work just before the interrupt "
          "is the likely wrong turn."),
```

- [ ] **Step 4: Rewrite and extend `sessionkit/detectors/lifecycle.py`**

Make these import changes:
- Change the harness import to
  `from sessionkit.harness import declared_timeout_ms, is_interrupt_marker, role`.
- Replace the `ParsedSession` import with
  `from sessionkit.parse import Message, ParsedSession, ToolCall`.

Then replace `stalls` with the code below, and add the new detectors after it:

```python
#: Their duration measures a human or a background job, not the tool.
_NO_STALL_ROLES = ("ask-user", "dispatch", "poll", "edit", "write")


def stalls(session: ParsedSession) -> list[Anomaly]:
    """Tool calls that ran far longer than they should have (spec §2.2)."""
    return [Anomaly("stall", f"{c.name} took {c.dur_ms // 1000}s", 1, [c.line])
            for c in session.tools if c.dur_ms is not None and _is_stall(session.harness, c)]


def _is_stall(harness: str, call: ToolCall) -> bool:
    call_role = role(harness, call.name)
    if call_role in _NO_STALL_ROLES:
        return False
    if call_role != "exec":
        return call.dur_ms > THRESHOLDS["stall_ms"]
    timeout = declared_timeout_ms(harness, call)
    if timeout:
        return call.dur_ms >= THRESHOLDS["stall_timeout_fraction"] * timeout
    return call.dur_ms > THRESHOLDS["stall_exec_ms"]


def hung(session: ParsedSession, live: bool) -> list[Anomaly]:
    """A finished (not live) session that ended waiting on a call or an unfinished turn."""
    if live:
        return []
    if session.end_state == "interrupted-tool":
        pending = [c for c in session.tools if c.err_class == "no-result"
                   and role(session.harness, c.name) not in ("ask-user", "dispatch")]
        if pending:
            return [Anomaly("hung", f"ended waiting on {pending[-1].name}", len(pending),
                            [c.line for c in pending][:MAX_LINES])]
    # Only pi persists an unfinished stream as an assistant message with no stop reason;
    # Claude Code writes stop_reason null on every intermediate per-block record.
    last = session.messages[-1] if session.messages else None
    if session.harness == "pi" and last is not None and last.role == "assistant" \
            and last.stop_reason == "":
        return [Anomaly("hung", "final model turn never finished streaming", 1, [last.line])]
    return []


def abrupt_truncation(session: ParsedSession, live: bool) -> list[Anomaly]:
    """The transcript's last line is a partial write and nothing is still writing it."""
    if live or not session.last_line_malformed:
        return []
    return [Anomaly("abrupt-truncation", "last transcript line is a partial write", 1, [])]


def interrupt_points(session: ParsedSession) -> list[int]:
    """Lines where the user stopped the agent: an aborted turn or call, or Claude Code's
    synthetic "[Request interrupted by user]" message."""
    points = {m.line for m in session.messages
              if m.role == "assistant" and m.stop_reason == "aborted"}
    points |= {c.line for c in session.tools if c.err_class == "aborted"}
    points |= {m.line for m in session.messages
               if m.role == "user" and is_interrupt_marker(m.preview)}
    return sorted(points)


def _next_prompt(session: ParsedSession, after: int) -> Message | None:
    return next((m for m in session.messages if m.role == "user" and m.line > after
                 and m.text_len > 0 and not is_interrupt_marker(m.preview)), None)


def user_interrupt(session: ParsedSession) -> list[Anomaly]:
    """The user stopped the agent and then told it something new — a redirect."""
    out: list[Anomaly] = []
    seen: set[int] = set()
    for point in interrupt_points(session):
        redirect = _next_prompt(session, point)
        if redirect is None or redirect.line in seen:
            continue
        seen.add(redirect.line)
        out.append(Anomaly("user-interrupt",
                           f"the user stopped the agent and redirected at line {redirect.line}",
                           1, [point, redirect.line]))
    return out
```

- [ ] **Step 5: Liveness and the new detectors in `detect`**

In `sessionkit/detectors/__init__.py`, append `lifecycle.user_interrupt,` to `DETECTORS`, then
replace `detect` with:

```python
def detect(session: ParsedSession, live: bool = False) -> list[Anomaly]:
    """Run every detector against a session.

    Args:
        session: A session whose errors and end state have already been derived.
        live: Whether the session is still running. A live session's unfinished tail is
            expected, so the hang and truncation detectors stay silent for it.

    Returns:
        Anomalies sorted by count, most frequent first.
    """
    found: list[Anomaly] = []
    for detector in DETECTORS:
        found.extend(detector(session))
    found.extend(lifecycle.hung(session, live))
    found.extend(lifecycle.abrupt_truncation(session, live))
    return sorted(found, key=lambda a: -a.count)
```

- [ ] **Step 6: New end states in `sessionkit/classify.py`**

Change the parse import to `from sessionkit.parse import Message, ParsedSession, ToolCall`,
then replace `derive_end_state` with:

```python
def derive_end_state(session: ParsedSession) -> tuple[str, str]:
    """Decide how a session ended, and why.

    Returns:
        ``(end_state, reason)`` where end_state is one of ``complete``, ``interrupted-tool``,
        ``killed-agents``, ``error-cascade``, ``aborted``, ``output-truncated``,
        ``compacted-idle``, ``agent-unresponsive``, ``interrupted-user`` or ``unknown``.
    """
    orphans = [c for c in session.tools if c.err_class == "no-result"]
    if orphans:
        names = ", ".join(sorted({c.name for c in orphans})[:3])
        return ("interrupted-tool", f"{len(orphans)} unresolved tool call(s): {names}")
    if any(e.subtype == "agents_killed" for e in session.sysev):
        return ("killed-agents", "a subagent was killed before returning")
    if trailing_errors(session.tools) >= THRESHOLDS["error_cascade"]:
        return ("error-cascade", "session ends on consecutive failing tool calls")
    if not session.messages:
        return ("unknown", "no messages parsed")
    last = session.messages[-1]
    if last.role == "assistant":
        return _assistant_end(session, last)
    if last.role == "user":
        return _user_end(session)
    return ("unknown", "no messages parsed")


_FINAL_STOPS = {
    "aborted": ("aborted", "the final model turn was aborted"),
    "length": ("output-truncated", "the final model turn hit the output token limit"),
}


def _assistant_end(session: ParsedSession, last: Message) -> tuple[str, str]:
    if last.stop_reason == "error":
        return ("error-cascade", last.error_text or "final model turn ended in an API error")
    if last.stop_reason in _FINAL_STOPS:
        return _FINAL_STOPS[last.stop_reason]
    if any(e.subtype == "compaction" for e in session.sysev[-5:]):
        return ("compacted-idle", "went idle shortly after a context compaction")
    return ("complete", "")


def _user_end(session: ParsedSession) -> tuple[str, str]:
    """A session ending on a user message: did the user walk away, or did the agent stop
    answering? The second blames the right party when the last model turn failed."""
    prior = next((m for m in reversed(session.messages[:-1]) if m.role == "assistant"), None)
    if prior is not None and prior.stop_reason in ("error", "aborted"):
        return ("agent-unresponsive",
                "the last user message followed an errored or aborted model turn and got no "
                "reply")
    return ("interrupted-user", "last turn is a user message with no reply")
```

- [ ] **Step 7: Pass liveness from the corpus**

In `sessionkit/corpus.py`:
- Add `from sessionkit import live` to the imports.
- Change `load_one`'s signature to
  `load_one(source, path, dir_name, live_sids: frozenset[str] = frozenset())`, and have it
  pass `live_sids` on: `return _load_pi(source, path, dir_name, live_sids)`.
- Change `_load_pi`'s signature to `_load_pi(source, path, dir_name, live_sids:
  frozenset[str])`.
- Change every `detect(x)` call to `detect(x, x.sid in live_sids)`. That is one call in
  `load_one`'s Claude Code path, and two in `_load_pi` (the parent and each child).

In `load()`, replace the loop header area with:

```python
    found = src.discover()
    corpus = Corpus(sources=found)
    scannable = src.scannable(found)
    live_set = frozenset(live.live_sids({s.root for s in scannable}))
    for source in scannable:
        if only and source.id != only:
            continue
        for path, dir_name in transcripts(source):
            try:
                corpus.sessions.extend(load_one(source, path, dir_name, live_set))
            except OSError:
                corpus.failed += 1
```

In `load_session()`, compute
`live_set = frozenset(live.live_sids({s.root for s in src.scannable(src.discover())}))` once
before its loop, and call `load_one(source, path, dir_name, live_set)`.

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/detectors sessionkit/classify.py sessionkit/corpus.py \
  sessionkit/taxonomy.py tests/test_detectors_lifecycle.py tests/test_classify.py
git commit -m "Detect hangs, truncated transcripts and user interrupts; add end states"
```

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

---
### Task 14: `sk forensics` becomes the post-mortem report

**Files:**
- Modify: `sessionkit/query.py`. Rewrite `forensics_findings` and `forensics_health`, and
  add `focus_profile_rows`.
- Modify: `sessionkit/cli.py`. Rewrite `cmd_forensics` and add
  `_forensics_health_section`.
- Test: `tests/test_corpus.py` (four new tests in `ForensicsTest`).

**Interfaces:**
- Consumes:
  - `taxonomy.lookup` and the `TIER_*` constants;
  - `detectors.alignment.focus_profile` (Task 12);
  - `detectors.generation.assistant_latencies` (Task 8).
- Produces the following. Task 17 documents all of these keys, and the skill reads them.
  - `forensics_findings` rows gain `tier`, `family` and `fix_target`. They are ordered
    `S`, then `S->J`, then `J`, and by count within each tier.
  - `forensics_health` gains `stop_reasons`, `empty_turns`, `reasoning_ratio`,
    `compactions` and `median_latency_s`.
  - `focus_profile_rows(entry)` returns rows with the keys `turn`, `ask`, `run`, `calls`,
    `share` and `referenced`.
  - The `sk forensics --json` tables are `findings`, `focus`, `timeline` and `health`.

- [ ] **Step 1: Write the failing tests**

Add to `ForensicsTest` in `tests/test_corpus.py`:

```python
    def _write_candidate_session(self) -> None:
        """The looping session, ending on a stated next step (a premature-stop candidate)."""
        records = [fx.user("do the thing")]
        for i in range(3):
            records.append(fx.assistant(
                [fx.tool_use(f"t{i}", "Bash", {"command": "flaky-check"})],
                ts=f"2026-08-01T00:00:0{i}Z"))
            records.append(fx.tool_result(f"t{i}", "still not ready",
                                          ts=f"2026-08-01T00:00:0{i}Z"))
        records.append(fx.assistant([{"type": "text", "text": "I'll add the tests next."}],
                                    ts="2026-08-01T00:00:09Z", stop_reason="end_turn"))
        fx.write(self.home, records, name="aaaa1111.jsonl")

    def test_findings_carry_tier_family_and_fix_target(self) -> None:
        self._write_looping_session()
        entry = next(iter(corpus.load().sessions))
        repeat = next(f for f in query.forensics_findings(entry) if f["kind"] == "repeat-tool")
        self.assertEqual((repeat["tier"], repeat["family"], repeat["fix_target"]),
                         ("S", "loop", "model"))

    def test_candidates_sort_after_structural_findings(self) -> None:
        self._write_candidate_session()
        entry = next(iter(corpus.load().sessions))
        tiers = [f["tier"] for f in query.forensics_findings(entry)]
        self.assertIn("S->J", tiers)
        order = ["S", "S->J", "J"]
        self.assertEqual(tiers, sorted(tiers, key=order.index))

    def test_health_reports_stop_reasons_and_latency(self) -> None:
        self._write_looping_session()
        health = query.forensics_health(next(iter(corpus.load().sessions)))
        self.assertIn("unrecorded", health["stop_reasons"])
        self.assertIsNotNone(health["median_latency_s"])
        self.assertEqual(health["compactions"], 0)

    def test_cli_report_has_focus_profile_and_candidate_note(self) -> None:
        self._write_candidate_session()
        loaded = corpus.load()
        out = cli.cmd_forensics(loaded, self._args("forensics", loaded.sessions[0].session.sid))
        self.assertIn("Focus profile", out)
        self.assertIn("candidate", out)
        self.assertIn("premature-stop", out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus.ForensicsTest -v`
Expected: FAIL with `KeyError: 'tier'` and `KeyError: 'stop_reasons'`, and because the output
has no "Focus profile" section.

- [ ] **Step 3: Update `sessionkit/query.py`**

Add `import statistics` to the imports, and change the taxonomy import to:

```python
from sessionkit.detectors.alignment import focus_profile
from sessionkit.detectors.generation import assistant_latencies
from sessionkit.taxonomy import TIER_CANDIDATE, TIER_J, TIER_S, lookup, signature
```

Replace `forensics_findings` and `forensics_health` with the following, and add
`focus_profile_rows`:

```python
_TIER_ORDER = {TIER_S: 0, TIER_CANDIDATE: 1, TIER_J: 2}


def forensics_findings(entry: Loaded) -> list[Row]:
    """One session's findings: structural first, then candidates, most frequent first within
    a tier. Each carries its taxonomy tier, family, fix target and hint."""
    rows = []
    for a in entry.anomalies:
        known = lookup(a.kind)
        rows.append({"kind": a.kind, "detail": a.detail, "count": a.count, "lines": a.lines,
                     "tier": known.tier if known else TIER_S,
                     "family": known.family if known else "",
                     "fix_target": known.fix_target if known else "",
                     "hint": known.hint if known else ""})
    rows.sort(key=lambda r: (_TIER_ORDER.get(r["tier"], len(_TIER_ORDER)), -r["count"],
                             r["kind"]))
    return rows


def forensics_health(entry: Loaded) -> Row:
    """The 'what went right' counterweight, plus how the model itself behaved."""
    session = entry.session
    tools = session.tools
    failed = sum(1 for t in tools if is_failure(t))
    assistants = [m for m in session.messages if m.role == "assistant"]
    stops: dict[str, int] = {}
    for message in assistants:
        key = message.stop_reason or "unrecorded"
        stops[key] = stops.get(key, 0) + 1
    visible = sum(m.text_len for m in assistants)
    reasoning = sum(m.reasoning_len for m in assistants)
    latencies = [ms for _m, ms in assistant_latencies(session)]
    return {
        "tool_calls": len(tools), "failed": failed, "succeeded": len(tools) - failed,
        "stop_reasons": stops,
        "empty_turns": sum(1 for m in assistants if m.stop_reason == "end"
                           and m.tool_calls == 0 and m.text_len == 0),
        "reasoning_ratio": round(reasoning / visible, 2) if visible else None,
        "compactions": sum(1 for e in session.sysev if e.subtype == "compaction"),
        "median_latency_s": int(statistics.median(latencies) // 1000) if latencies else None,
    }


def focus_profile_rows(entry: Loaded) -> list[Row]:
    """Per user turn, where the effort went. A turn holding a drift candidate is expanded
    into its runs; every other turn is one summary row, so long sessions stay in budget."""
    flagged = {line for a in entry.anomalies if a.kind == "drift-candidate" for line in a.lines}
    rows: list[Row] = []
    for turn in focus_profile(entry.session):
        expanded = turn.calls and any(flagged.intersection(r.lines) for r in turn.runs)
        if not expanded:
            rows.append({"turn": turn.ask_line, "ask": turn.ask_preview, "run": "-",
                         "calls": turn.calls, "share": "", "referenced": ""})
            continue
        for run in turn.runs:
            rows.append({"turn": turn.ask_line, "ask": turn.ask_preview, "run": run.key,
                         "calls": len(run.lines), "share": f"{len(run.lines) / turn.calls:.0%}",
                         "referenced": "yes" if run.referenced else "no"})
    return rows
```

- [ ] **Step 4: Rewrite `cmd_forensics` in `sessionkit/cli.py`**

Change the taxonomy import to `from sessionkit.taxonomy import TIER_CANDIDATE,
classify_error`. Replace `cmd_forensics` with the following, and add the helper after it:

```python
def cmd_forensics(corpus: Corpus, args: argparse.Namespace) -> str:
    """Why one session went wrong: tiered findings, the focus profile, an anchored timeline,
    and health.

    Deterministic by design — findings and hints are looked up from the detectors and
    ``taxonomy``, not generated, so this is a report, not a narrative. The narrative is the
    ``session-forensics`` skill's job.
    """
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    session = entry.session
    findings = query.forensics_findings(entry)

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB)
    report.meta(sid=session.sid, project=entry.project_key, harness=session.harness or "-",
                model=session.model or "-", state=session.end_state, findings=len(findings))
    if session.end_reason:
        report.text(f"End reason: {session.end_reason}")

    report.section("Findings")
    report.table(
        ["tier", "family", "kind", "detail", "count", "lines", "fix_target", "hint"],
        [["candidate" if f["tier"] == TIER_CANDIDATE else f["tier"], f["family"], f["kind"],
          f["detail"], f["count"], ",".join(str(n) for n in f["lines"]), f["fix_target"],
          f["hint"]] for f in findings],
        key="findings",
    )
    if any(f["tier"] == TIER_CANDIDATE for f in findings):
        report.text("Rows marked `candidate` are hypotheses: confirm or dismiss each against "
                    "its cited lines before reporting it.")
    report.section("Focus profile")
    report.table(
        ["turn", "ask", "run", "calls", "share", "referenced"],
        [[r["turn"], r["ask"], r["run"], r["calls"], r["share"], r["referenced"]]
         for r in query.focus_profile_rows(entry)],
        key="focus",
    )
    report.section("Timeline (finding-anchored)")
    report.table(
        ["line", "kind", "what", "detail"],
        [[r["line"], r["kind"], r["name"], r["detail"] or ""]
         for r in query.forensics_timeline(entry)],
        key="timeline",
    )
    _forensics_health_section(report, query.forensics_health(entry))
    return report.render()


def _forensics_health_section(report: Report, health: query.Row) -> None:
    """Tool success plus model behaviour: stop reasons, empty turns, reasoning, latency."""
    stops = ", ".join(f"{k}={v}" for k, v in sorted(health["stop_reasons"].items())) or "-"
    ratio = health["reasoning_ratio"]
    latency = health["median_latency_s"]
    report.section("Health")
    report.table(
        ["metric", "value"],
        [["tool calls", health["tool_calls"]], ["succeeded", health["succeeded"]],
         ["failed", health["failed"]],
         ["success rate", _pct(health["succeeded"], health["tool_calls"])],
         ["stop reasons", stops], ["empty turns", health["empty_turns"]],
         ["reasoning/visible", "-" if ratio is None else ratio],
         ["compactions", health["compactions"]],
         ["median turn latency", "-" if latency is None else f"{latency}s"]],
        key="health",
    )
```

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS. The existing `ForensicsTest` tests still pass, because the "(none)",
"succeeded", "repeat-tool" and "Health" strings are all still present.

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_corpus.py
git commit -m "Make sk forensics a tiered post-mortem report with a focus profile"
```

---

### Task 15: `sk findings` fleet view, `errors --group-by model|harness`, and `index --finding`

**Files:**
- Modify: `sessionkit/query.py`. Add `findings_rows`, `_findings_by_kind`, `_FINDING_GROUP`
  and `_group_sizes`. `clusters` gains `sessions_in_group`, `_GROUP_KEY` gains `model` and
  `harness`, and `Filter` gains `finding`.
- Modify: `sessionkit/cli.py`. Add `cmd_findings`, register it in `COMMANDS`, and add its
  parser. Also change `cmd_errors`, the `--group-by` choices, `_scope`, and add
  `p_index --finding`.
- Test: `tests/test_corpus.py` (new class `FindingsFleetTest`).

**Interfaces:**
- Consumes `Loaded.anomalies`, `taxonomy.lookup`, and the `ParsedSession.model` and
  `harness` fields.
- Produces:
  - `query.findings_rows(corpus, scope, group_by, tier="", family="", kind="") ->
    list[Row]`. Each row has `group` (or `kind`, `tier` and `family` when `group_by="kind"`),
    `occurrences`, `sessions_affected`, `sessions_in_group` and `rate`.
  - `query.Filter.finding`.
  - `sk findings --group-by kind|model|harness|project|session --tier --family --kind`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_corpus.py` (before the `if __name__` line):

```python
class FindingsFleetTest(unittest.TestCase):
    """`sk findings` and the model/harness groupings: rates, not raw counts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "PI_DIR": "/nonexistent/pi",
            "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)
        looping = [fx.user("do the thing")]
        for i in range(3):
            looping.append(fx.assistant([fx.tool_use(f"t{i}", "Bash", {"command": "check"})],
                                        ts=f"2026-08-01T00:00:0{i}Z", model="claude-opus-5"))
            looping.append(fx.tool_result(f"t{i}", "not ready", ts=f"2026-08-01T00:00:0{i}Z"))
        fx.write(self.home, looping, name="aaaa1111.jsonl")
        fx.write(self.home, [
            fx.user("check it"),
            fx.assistant([fx.tool_use("u1", "Bash", {"command": "false"})],
                         model="claude-sonnet-5"),
            fx.tool_result("u1", "Exit code 1", is_error=True),
            fx.assistant([{"type": "text", "text": "ok"}], ts="2026-08-01T00:00:05Z",
                         model="claude-sonnet-5"),
        ], name="bbbb2222.jsonl")
        self.loaded = corpus.load()

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_rates_by_model(self) -> None:
        rows = {r["group"]: r for r in query.findings_rows(self.loaded, query.Filter(), "model",
                                                           kind="repeat-tool")}
        self.assertEqual(rows["claude-opus-5"]["rate"], 1.0)
        self.assertEqual(rows["claude-sonnet-5"]["rate"], 0.0)
        self.assertEqual(rows["claude-sonnet-5"]["sessions_in_group"], 1)

    def test_by_kind_uses_the_whole_scope_as_denominator(self) -> None:
        rows = {r["kind"]: r for r in query.findings_rows(self.loaded, query.Filter(), "kind")}
        self.assertEqual(rows["repeat-tool"]["sessions_affected"], 1)
        self.assertEqual(rows["repeat-tool"]["sessions_in_group"], 2)
        self.assertEqual(rows["repeat-tool"]["tier"], "S")

    def test_index_filter_by_finding(self) -> None:
        rows = query.index_rows(self.loaded, query.Filter(finding="repeat-tool"))
        self.assertEqual([r["sid"] for r in rows],
                         [e.session.sid for e in self.loaded.sessions
                          if any(a.kind == "repeat-tool" for a in e.anomalies)])
        self.assertEqual(len(rows), 1)

    def test_cli_findings_report(self) -> None:
        out = cli.cmd_findings(self.loaded, self._args("findings", "--group-by", "model"))
        self.assertIn("claude-opus-5", out)
        self.assertIn("rate", out)

    def test_cli_errors_by_harness_shows_reach(self) -> None:
        out = cli.cmd_errors(self.loaded, self._args("errors", "--group-by", "harness"))
        self.assertIn("claude-code", out)
        self.assertIn("reach", out)
        self.assertIn("50.0%", out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus.FindingsFleetTest -v`
Expected: FAIL with `AttributeError: module 'sessionkit.query' has no attribute
'findings_rows'`.

- [ ] **Step 3: Update `sessionkit/query.py`**

In `Filter`, add the field `finding: str = ""`. In `Filter.matches`, add this before
`return True`:

```python
        if self.finding and not any(a.kind == self.finding for a in entry.anomalies):
            return False
```

Also update the `Filter` docstring to mention `--finding`.

Add these two entries to `_GROUP_KEY`:

```python
    "model": lambda e, t: e.session.model or "-",
    "harness": lambda e, t: e.session.harness or "-",
```

In `clusters`, compute `scoped = scope.apply(corpus)` and `sizes = _group_sizes(scoped,
group_by)`, and iterate over `_failures(scoped)`. Add
`"sessions_in_group": sizes.get(b["bucket"], len(scoped))` to each output row.

Add, after `clusters`:

```python
#: Per-session grouping keys shared by `sk findings` and the model/harness error groupings.
_FINDING_GROUP: dict[str, Callable[[Loaded], str]] = {
    "model": lambda e: e.session.model or "-",
    "harness": lambda e: e.session.harness or "-",
    "project": lambda e: e.project_key,
    "session": lambda e: e.session.sid,
}


def _group_sizes(scoped: list[Loaded], group_by: str) -> dict[str, int]:
    """Sessions per model or harness — the denominator a per-group rate needs."""
    if group_by not in ("model", "harness"):
        return {}
    key_of = _FINDING_GROUP[group_by]
    sizes: dict[str, int] = {}
    for entry in scoped:
        key = key_of(entry)
        sizes[key] = sizes.get(key, 0) + 1
    return sizes


def _finding_wanted(anomaly: Any, tier: str, family: str, kind: str) -> bool:
    known = lookup(anomaly.kind)
    if kind and anomaly.kind != kind:
        return False
    if tier and (known is None or known.tier != tier):
        return False
    return not family or (known is not None and known.family == family)


def findings_rows(corpus: Corpus, scope: Filter, group_by: str, tier: str = "",
                  family: str = "", kind: str = "") -> list[Row]:
    """Detector findings across the fleet, as rates: sessions affected out of sessions in the
    group. Without the denominator, whichever model is used most always looks worst."""
    entries = scope.apply(corpus)
    if group_by == "kind":
        return _findings_by_kind(entries, tier, family, kind)
    key_of = _FINDING_GROUP[group_by]
    groups: dict[str, Row] = {}
    for entry in entries:
        key = key_of(entry)
        row = groups.setdefault(key, {"group": key, "occurrences": 0, "sessions_affected": 0,
                                      "sessions_in_group": 0})
        row["sessions_in_group"] += 1
        hits = [a for a in entry.anomalies if _finding_wanted(a, tier, family, kind)]
        if hits:
            row["sessions_affected"] += 1
            row["occurrences"] += sum(a.count for a in hits)
    rows = list(groups.values())
    for row in rows:
        row["rate"] = row["sessions_affected"] / row["sessions_in_group"]
    rows.sort(key=lambda r: (-r["rate"], -r["occurrences"], r["group"]))
    return rows


def _findings_by_kind(entries: list[Loaded], tier: str, family: str, kind: str) -> list[Row]:
    rows: dict[str, Row] = {}
    for entry in entries:
        seen: set[str] = set()
        for anomaly in entry.anomalies:
            if not _finding_wanted(anomaly, tier, family, kind):
                continue
            known = lookup(anomaly.kind)
            row = rows.setdefault(anomaly.kind, {
                "kind": anomaly.kind, "tier": known.tier if known else "",
                "family": known.family if known else "", "occurrences": 0,
                "sessions_affected": 0})
            row["occurrences"] += anomaly.count
            if anomaly.kind not in seen:
                seen.add(anomaly.kind)
                row["sessions_affected"] += 1
    total = len(entries)
    out = list(rows.values())
    for row in out:
        row["sessions_in_group"] = total
        row["rate"] = row["sessions_affected"] / total if total else 0.0
    out.sort(key=lambda r: (-r["sessions_affected"], -r["occurrences"], r["kind"]))
    return out
```

- [ ] **Step 4: Update `sessionkit/cli.py`**

In `_scope`, add `finding=getattr(args, "finding", None) or "",` to the `query.Filter(...)`
call.

In `cmd_errors`, replace the `report.table(...)` call for the clusters with:

```python
    reach = args.group_by in ("model", "harness")
    report.table(
        ["bucket", "count", "share", "sessions"] + (["reach"] if reach else [])
        + ["fix", "exemplar"],
        [[r["bucket"] or "-", r["n"], _pct(r["n"], total), r["sessions"]]
         + ([_pct(r["sessions"], r["sessions_in_group"])] if reach else [])
         + [classify_error(r["exemplar"])[1], r["exemplar"] or ""] for r in rows],
        key="clusters",
    )
```

Add after `cmd_errors`:

```python
def cmd_findings(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 2: detector findings across the fleet, as rates per kind, model, harness,
    project or session — the input for "what does this model keep getting wrong"."""
    scope = _scope(args)
    rows = query.findings_rows(corpus, scope, args.group_by, args.tier or "",
                               args.family or "", args.kind or "")
    total = query.session_count(corpus, scope)
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(sessions=total, grouped_by=args.group_by)
    report.section(f"Findings by {args.group_by}")
    if args.group_by == "kind":
        report.table(["kind", "tier", "family", "occurrences", "sessions", "rate"],
                     [[r["kind"], r["tier"], r["family"], r["occurrences"],
                       r["sessions_affected"], _pct(r["sessions_affected"], total)]
                      for r in rows], key="findings")
    else:
        report.table(["group", "occurrences", "sessions_affected", "sessions_in_group", "rate"],
                     [[r["group"], r["occurrences"], r["sessions_affected"],
                       r["sessions_in_group"], _pct(r["sessions_affected"],
                                                    r["sessions_in_group"])]
                      for r in rows], key="findings")
    if args.group_by == "model":
        report.text("Grouped by each session's last model; a session that switched models "
                    "counts under the final one.")
    if any(r.get("sessions_in_group", total) < 10 for r in rows):
        report.text("Groups with fewer than 10 sessions: their rates are not meaningful yet.")
    return report.render()
```

In `build_parser`, make these changes:
1. Change `p_err`'s `--group-by` choices to
   `["class", "tool", "signature", "session", "model", "harness"]`.
2. Add `p_index.add_argument("--finding", help="only sessions with this finding kind, e.g.
   drift-candidate")`.
3. Add the new subparser after `p_err`:

```python
    p_find = sub.add_parser("findings", parents=[common_sub, scoped],
                            help="detector findings fleet-wide, as rates (layer 2)")
    p_find.add_argument("--group-by", choices=["kind", "model", "harness", "project", "session"],
                        default="kind")
    p_find.add_argument("--tier", choices=["S", "S->J", "J"])
    p_find.add_argument("--family", choices=["tool-protocol", "generation", "loop",
                                             "lifecycle", "recovery", "alignment",
                                             "environment"])
    p_find.add_argument("--kind", help="one finding kind, e.g. degeneration")
    _subagents_arg(p_find, "exclude")
```

Add `"findings": cmd_findings` to `COMMANDS`.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_corpus.py
git commit -m "Add sk findings, per-model/harness error rates, and index --finding"
```

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
