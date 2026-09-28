# Checkpoint 3: Core detectors

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this checkpoint task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Part of:** the agent post-mortem plan. Tracker: `2026-09-27-agent-postmortem-00-tracker.md`. Read its **Global Constraints**, **Spec deviations** and **Review Focus** before starting; they apply to every task here.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**Goal:** The new and widened error classes; the tool-protocol, generation, loop and lifecycle detectors; the new end states; liveness passed into the detectors.

**Starts from:** Checkpoint 2 is complete: its tasks are ticked in the tracker and the full suite passes.

**Done when:** every task below is committed; `PYTHONPATH=. python3 -m unittest discover -s tests -t .` passes; and the tracker marks each task done with its commit.

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
