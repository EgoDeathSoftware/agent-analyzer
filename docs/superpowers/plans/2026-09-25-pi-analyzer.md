# pi Session Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sessionkit` discover and parse `pi` (`@earendil-works/pi-coding-agent`) sessions
so every existing `sk` command (`doctor`, `index`, `show`, `errors`, `cost`, `tail`, `search`,
`children`, ...) covers them automatically, alongside Claude Code and opencode.

**Architecture:** A third `Source.kind` ("pi") in `sources.py`, a new parser module
(`sessionkit/parse_pi.py`) that turns pi's tree-shaped JSONL into the *same*
`ParsedSession`/`Message`/`ToolCall`/`FileOp`/`SysEvent` dataclasses `parse.py` already
produces for Claude Code, and a `corpus.py` dispatch by `source.kind`. Because every downstream
module (`classify.py`, `query.py`, `render.py`, `cli.py`) already only reads that generic model,
no new commands or skills are needed — two literal `"Agent"`-tool-name checks widen to also
match pi's `"subagent"` tool, and two skill files lose their Claude-Code-only wording.

**Tech Stack:** Python 3.11+ stdlib only (no third-party imports), `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-25-pi-analyzer-design.md`

## Global Constraints

- System Python 3.11+, zero third-party imports (project-wide, per `README.md`).
- No new CLI subcommands. `--source pi` must work through the *existing* free-string filter in
  `query.Filter.matches` (spec §6) — do not add a hardcoded source-kind enum anywhere.
- No changes to `sessionkit/pricing.py`. pi's per-message `usage.cost.total` is used directly;
  no Claude-Code pricing table lookup happens for pi sessions (spec §3.5).
- `ParsedSession`/`Message`/`ToolCall`/`FileOp`/`SysEvent` keep their existing fields unchanged.
  The only additive change across the whole shared model is one new field,
  `ParsedSession.child_sessions: list[ParsedSession]`, default `[]` (spec §3, §4).
- `classify.py` and `query.py` change in exactly two places: the literal string `"Agent"` in
  `classify._agent_trouble` and `query.children_rows` each widen to also match `"subagent"`.
  No new anomaly detectors, no new query helpers (spec §5).
- Entries are parsed in file order; there is no active-leaf-path (tree-walk) resolution in this
  pass (spec §3.4) — a full-tree parse, documented as a known simplification.
- Tests are built from inline fixtures (a `tests/fixtures_pi.py` builder module), never
  committed `.jsonl` files, matching the existing `tests/fixtures.py` convention. Run with
  `PYTHONPATH=. python3 -m unittest discover -s tests -t .` from the repo root.
- `opencode` support is untouched — still aspirational, not part of this work.

## Review Focus

- An unrecognized/future pi entry `type` (schema drift) must count in `malformed_lines`, not
  raise and not vanish silently — Task 2.
- A `toolResult.toolCallId` matching no pending call (a corrupted or out-of-order file) must be
  ignored, not crash and not falsely close an unrelated call — Task 2.
- A `subagent`-named `toolResult` whose `details` is missing or whose `details.results` is
  empty must produce zero synthetic children, not crash and not synthesize a bogus child —
  Task 3.
- An `assistant` message with no `usage` field (or a partially-populated one) must leave
  tokens/cost at `0`, never raise on a missing key — Task 2.
- A real `pi -p "..."` run's actual session file must parse cleanly (`malformed_lines == 0`,
  no exception) — this is the spec's own stated acceptance gap (§1, §8), not something the
  fixture-based tests in Tasks 2–5 can close on their own — Task 7.

---

### Task 1: pi source discovery

**Files:**
- Modify: `sessionkit/sources.py`
- Test: `tests/test_sources_pi.py` (new)

**Interfaces:**
- Produces: `Source.sessions_root` property (used by Task 4); a `Source` with `kind="pi"`
  appearing unconditionally in `discover()`'s result, keyed `id="pi"`, reachable when
  `<PI_DIR>/sessions` is a directory.

pi has no `projects/` indirection — `sessions/--<encoded-cwd>--/<file>.jsonl` sits directly under
`<PI_DIR>/sessions`. Unlike the optional `ENV_KEYS` sources (opencode, agents store), pi should
be probed by default the same way the Claude Code `host` source already is, without requiring
an env var — this is a deliberate refinement of spec §2's literal wording, which suggested an
`ENV_KEYS` entry; an `ENV_KEYS` source only appears when its env var is set, and there is no
reason pi should need an opt-in that Claude Code's own default source doesn't.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for pi source discovery."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionkit import sources as src


class PiSourceTest(unittest.TestCase):
    """Discovering the pi session store as a third Source kind."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.tmp / "claude"),  # left absent: unreachable, on purpose
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pi_source_unreachable_without_sessions_dir(self) -> None:
        pi_home = self.tmp / "pi-agent"
        pi_home.mkdir()
        with mock.patch.dict(os.environ, {"PI_DIR": str(pi_home)}):
            found = {s.id: s for s in src.discover()}
        self.assertIn("pi", found)
        self.assertEqual(found["pi"].kind, "pi")
        self.assertFalse(found["pi"].reachable)
        self.assertEqual(found["pi"].note, "no sessions/ subdirectory")

    def test_pi_source_reachable_with_sessions_dir(self) -> None:
        pi_home = self.tmp / "pi-agent"
        (pi_home / "sessions").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"PI_DIR": str(pi_home)}):
            found = {s.id: s for s in src.discover()}
        self.assertTrue(found["pi"].reachable)
        self.assertEqual(found["pi"].note, "")

    def test_pi_source_is_scannable_when_reachable(self) -> None:
        pi_home = self.tmp / "pi-agent"
        (pi_home / "sessions").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"PI_DIR": str(pi_home)}):
            scannable_ids = {s.id for s in src.scannable(src.discover())}
        self.assertIn("pi", scannable_ids)

    def test_pi_source_defaults_to_home_pi_agent_without_env_var(self) -> None:
        # No PI_DIR set at all: discover() must still produce a "pi" candidate (unreachable is
        # fine, since ~/.pi/agent/sessions is very unlikely to exist under a temp HOME), proving
        # pi is probed by default like the Claude Code "host" source, not opt-in like opencode.
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}, clear=False):
            found = {s.id: s for s in src.discover()}
        self.assertIn("pi", found)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_sources_pi -v`
Expected: FAIL — no `Source` with `id="pi"` is produced yet (`AssertionError` on
`self.assertIn("pi", found)`), and `Source` has no `sessions_root` (not yet referenced, so no
error there — the failure is purely "pi never appears").

- [ ] **Step 3: Add the `sessions_root` property**

In `sessionkit/sources.py`, immediately after the existing `projects_dir` property on `Source`:

```python
    @property
    def sessions_root(self) -> Path:
        """Directory holding per-project session folders, for a pi source."""
        return self.root / "sessions"
```

- [ ] **Step 4: Probe pi sources**

In `_probe()`, add a branch before the `store-set` check (pi has no `store-set` layout, so
placement relative to that branch doesn't matter, but placing it next to the `opencode` branch
groups the two kind-specific probes together):

```python
    if source.kind == "opencode":
        db = source.root / "opencode.db"
        source.reachable = db.is_file()
        source.note = "" if source.reachable else "no opencode.db"
        return source
    if source.kind == "pi":
        source.reachable = source.sessions_root.is_dir()
        source.note = "" if source.reachable else "no sessions/ subdirectory"
        return source
```

- [ ] **Step 5: Make pi a default, always-probed candidate**

In `_candidates()`, add a second unconditional entry alongside `host`, and read `PI_DIR` the
same way `CLAUDE_DIR` is already read:

```python
def _candidates(env: dict[str, str]) -> list[Source]:
    """Build the unprobed candidate list from env configuration."""
    host_root = Path(env.get("CLAUDE_DIR") or Path.home() / ".claude").expanduser()
    pi_root = Path(env.get("PI_DIR") or Path.home() / ".pi" / "agent").expanduser()
    found = [
        Source(id="host", kind="claude-code", layout="single",
               location="host", root=host_root),
        Source(id="pi", kind="pi", layout="single",
               location="host", root=pi_root),
    ]
    for key, (sid, kind, layout) in ENV_KEYS.items():
        value = env.get(key)
        if value:
            found.append(Source(id=sid, kind=kind, layout=layout,
                                location="host", root=Path(value).expanduser()))
    return found
```

- [ ] **Step 6: Pass `PI_DIR` through from the real environment**

In `load_env()`, extend the explicit pass-through tuple:

```python
    for key in (*ENV_KEYS, "CLAUDE_DIR", "PI_DIR"):
        if key in os.environ:
            values[key] = os.environ[key]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_sources_pi -v`
Expected: PASS (all 4 tests)

- [ ] **Step 8: Run the full existing suite to check nothing broke**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: PASS — in particular `tests/test_corpus.py`'s tests, which patch only `CLAUDE_DIR`
and `SESSIONKIT_ENV`; pi's default `~/.pi/agent` will be unreachable in that environment (no
`PI_DIR` override, real `$HOME` unlikely to have `.pi/agent/sessions` during CI), so it must
not add any session to those tests' corpora or break their session counts.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/sources.py tests/test_sources_pi.py
git commit -m "Add pi as a discoverable session source"
```

---

### Task 2: pi parser core

**Files:**
- Create: `sessionkit/parse_pi.py`
- Create: `tests/fixtures_pi.py`
- Test: `tests/test_parse_pi.py` (new)

**Interfaces:**
- Consumes: `sessionkit.parse.{ParsedSession, Message, ToolCall, FileOp, SysEvent, digest}`
  (existing, unchanged signatures); `sessionkit.redact.preview(text: str, limit: int) -> str`
  (existing).
- Produces: `parse_pi.parse_file(path: Path, source_id: str) -> ParsedSession` (used by Task 4).

This task builds the core mapping (§3.1–3.3, §3.5 of the spec) — plain user/assistant messages,
`toolCall`/`toolResult` pairing, `bashExecution`, file ops, cost/tokens, and the sysevent
entries. Subagent nesting (§4) is Task 3, kept separate because a reviewer could reasonably
approve this task's mapping while rejecting the nesting logic, or vice versa.

One documented discrepancy in the source docs: `SessionEntryBase.timestamp` is an ISO string,
always present, but the *nested* `AgentMessage.timestamp` the docs also describe is a Unix-ms
number and is inconsistently present in the docs' own JSON examples. This parser uses the
entry-level ISO `timestamp` as the single source of truth for every `ts` field below — it is
guaranteed present and matches the ISO-string convention every other field in `ParsedSession`
already uses. The message-level Unix-ms `timestamp` is only read in Task 3, where nested
subagent messages have no entry wrapper to fall back on.

- [ ] **Step 1: Write `tests/fixtures_pi.py`**

```python
"""Builders for synthetic pi (@earendil-works/pi-coding-agent) session JSONL.

Mirrors tests/fixtures.py's role for Claude Code transcripts: fixtures are constructed inline
so each test states the exact shape it depends on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SID = "aaaa1111-bbbb-2222-cccc-3333dddd4444"
CWD = "/home/dev/myproject"


def header(ts: str = "2026-09-01T00:00:00.000Z", sid: str = SID, **extra: Any) -> dict[str, Any]:
    """The first line of a pi session file: metadata only, no id/parentId."""
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": CWD, **extra}


def _entry(etype: str, entry_id: str, parent_id: str | None, ts: str,
          **extra: Any) -> dict[str, Any]:
    return {"type": etype, "id": entry_id, "parentId": parent_id, "timestamp": ts, **extra}


def user_msg(entry_id: str, parent_id: str | None, text: str,
            ts: str = "2026-09-01T00:00:01.000Z") -> dict[str, Any]:
    return _entry("message", entry_id, parent_id, ts,
                 message={"role": "user", "content": text})


def assistant_msg(entry_id: str, parent_id: str | None, content: list[dict[str, Any]],
                  ts: str = "2026-09-01T00:00:02.000Z", model: str = "claude-sonnet-5",
                  usage: dict[str, Any] | None = None, stop_reason: str = "stop"
                  ) -> dict[str, Any]:
    return _entry("message", entry_id, parent_id, ts, message={
        "role": "assistant", "content": content, "provider": "anthropic", "model": model,
        "usage": usage or {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0,
                           "totalTokens": 150,
                           "cost": {"input": 0.001, "output": 0.001, "cacheRead": 0.0,
                                    "cacheWrite": 0.0, "total": 0.002}},
        "stopReason": stop_reason,
    })


def tool_call_block(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def tool_result_msg(entry_id: str, parent_id: str | None, call_id: str, tool_name: str,
                    text: str, is_error: bool = False, details: dict[str, Any] | None = None,
                    ts: str = "2026-09-01T00:00:03.000Z") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "toolResult", "toolCallId": call_id, "toolName": tool_name,
                           "content": [{"type": "text", "text": text}], "isError": is_error}
    if details is not None:
        msg["details"] = details
    return _entry("message", entry_id, parent_id, ts, message=msg)


def bash_msg(entry_id: str, parent_id: str | None, command: str, output: str,
            exit_code: int | None = 0, cancelled: bool = False,
            ts: str = "2026-09-01T00:00:03.000Z") -> dict[str, Any]:
    return _entry("message", entry_id, parent_id, ts, message={
        "role": "bashExecution", "command": command, "output": output, "exitCode": exit_code,
        "cancelled": cancelled, "truncated": False,
    })


def compaction(entry_id: str, parent_id: str | None, summary: str, tokens_before: int,
              ts: str = "2026-09-01T00:00:04.000Z") -> dict[str, Any]:
    return _entry("compaction", entry_id, parent_id, ts, summary=summary,
                 tokensBefore=tokens_before)


def write(tmp: Path, entries: list[dict[str, Any]],
         name: str = f"20260901T000000_{SID}.jsonl") -> Path:
    """Write entries as JSONL under pi's own layout: sessions/--<encoded-cwd>--/<file>."""
    folder = tmp / "sessions" / "--home-dev-myproject--"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def simple_session() -> list[dict[str, Any]]:
    """header -> user -> assistant(toolCall) -> toolResult -> assistant(text)."""
    return [
        header(),
        user_msg("e1", None, "read a file"),
        assistant_msg("e2", "e1", [tool_call_block("c1", "read", {"path": "/a.py"})]),
        tool_result_msg("e3", "e2", "c1", "read", "file contents"),
        assistant_msg("e4", "e3", [{"type": "text", "text": "done"}]),
    ]
```

- [ ] **Step 2: Write the failing tests**

```python
"""Tests for pi (@earendil-works/pi-coding-agent) transcript parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sessionkit.parse_pi import parse_file
from tests import fixtures_pi as fx


class ParsePiTest(unittest.TestCase):
    """Parsing a pi session file into the normalised event stream."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parse(self, entries: list[dict], **kw) -> "object":
        return parse_file(fx.write(self.tmp, entries, **kw), "pi")

    def test_header_sets_sid_and_cwd(self) -> None:
        session = self.parse(fx.simple_session())
        self.assertEqual(session.sid, fx.SID)
        self.assertEqual(session.cwd, fx.CWD)

    def test_pairs_tool_call_with_result(self) -> None:
        session = self.parse(fx.simple_session())
        self.assertEqual(len(session.tools), 1)
        call = session.tools[0]
        self.assertEqual(call.name, "read")
        self.assertFalse(call.is_error)
        self.assertEqual(call.err_class, "")
        self.assertIn("file contents", call.output_preview)

    def test_records_file_op_from_toolcall(self) -> None:
        session = self.parse(fx.simple_session())
        self.assertEqual([(f.path, f.op) for f in session.files], [("/a.py", "read")])

    def test_counts_user_turn_and_first_prompt(self) -> None:
        session = self.parse(fx.simple_session())
        self.assertEqual(session.turns, 1)
        self.assertEqual(session.first_prompt, "read a file")

    def test_accumulates_usage_and_cost(self) -> None:
        # simple_session() has two assistant turns, 100 tok_in / 50 tok_out / $0.002 each.
        session = self.parse(fx.simple_session())
        self.assertEqual(session.tok_in, 200)
        self.assertEqual(session.tok_out, 100)
        self.assertAlmostEqual(session.cost_usd, 0.004)

    def test_unresolved_tool_call_is_flagged(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.assistant_msg("e2", "e1",
                             [fx.tool_call_block("c1", "bash", {"command": "sleep 1"})]),
        ])
        self.assertEqual(session.tools[0].err_class, "no-result")

    def test_error_result_marked(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.assistant_msg("e2", "e1",
                             [fx.tool_call_block("c1", "bash", {"command": "grep x"})]),
            fx.tool_result_msg("e3", "e2", "c1", "bash",
                               "BLOCKED: Use rg instead of grep", is_error=True),
        ])
        self.assertTrue(session.tools[0].is_error)

    def test_bash_execution_is_a_tool_call(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.bash_msg("e2", "e1", "ls -la", "total 0\n", exit_code=0),
        ])
        self.assertEqual(len(session.tools), 1)
        self.assertEqual(session.tools[0].name, "bash")
        self.assertFalse(session.tools[0].is_error)

    def test_bash_execution_nonzero_exit_is_an_error(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.bash_msg("e2", "e1", "false", "", exit_code=1),
        ])
        self.assertTrue(session.tools[0].is_error)

    def test_bash_execution_cancelled_is_an_error(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.bash_msg("e2", "e1", "sleep 60", "", exit_code=None, cancelled=True),
        ])
        self.assertTrue(session.tools[0].is_error)

    def test_compaction_recorded_as_sysevent(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.assistant_msg("e2", "e1", [{"type": "text", "text": "hi"}]),
            fx.compaction("e3", "e2", "earlier context...", 50000),
        ])
        self.assertEqual([e.subtype for e in session.sysev], ["compaction"])

    # --- Review Focus: unrecognized entry type -----------------------------------------
    def test_unknown_entry_type_counts_as_malformed_not_fatal(self) -> None:
        session = self.parse([
            fx.header(),
            {"type": "future_entry_type", "id": "e1", "parentId": None,
             "timestamp": "2026-09-01T00:00:01.000Z", "somethingNew": True},
            fx.user_msg("e2", "e1", "go"),
        ])
        self.assertEqual(session.malformed_lines, 1)
        self.assertEqual(session.turns, 1)

    # --- Review Focus: unmatched toolResult ---------------------------------------------
    def test_tool_result_with_unknown_call_id_is_ignored(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.tool_result_msg("e2", "e1", "no-such-call", "bash", "stray output"),
        ])
        self.assertEqual(session.tools, [])

    # --- Review Focus: assistant message with no usage ----------------------------------
    def test_assistant_message_without_usage_defaults_to_zero(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            {"type": "message", "id": "e2", "parentId": "e1",
             "timestamp": "2026-09-01T00:00:02.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
                        "provider": "anthropic", "model": "claude-sonnet-5",
                        "stopReason": "stop"}},
        ])
        self.assertEqual(session.tok_in, 0)
        self.assertEqual(session.cost_usd, 0.0)

    # --- Review Focus: forked session header --------------------------------------------
    def test_forked_session_header_is_not_followed(self) -> None:
        session = self.parse([
            fx.header(parentSession="/some/other/session.jsonl"),
            fx.user_msg("e1", None, "go"),
        ])
        self.assertEqual(session.cwd, fx.CWD)
        self.assertEqual(session.turns, 1)

    def test_malformed_json_line_is_counted(self) -> None:
        path = fx.write(self.tmp, [fx.header(), fx.user_msg("e1", None, "go")])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not valid json\n")
        session = parse_file(path, "pi")
        self.assertEqual(session.malformed_lines, 1)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse_pi -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sessionkit.parse_pi'`

- [ ] **Step 4: Write `sessionkit/parse_pi.py`**

```python
"""pi (@earendil-works/pi-coding-agent) JSONL session -> normalised event stream.

One pass per file produces a :class:`~sessionkit.parse.ParsedSession`, reusing the same
dataclasses `sessionkit.parse` builds for Claude Code transcripts so every downstream consumer
(classify, query, render) needs no pi-specific code.

Record shapes are taken from the package's own documentation
(`node_modules/@earendil-works/pi-coding-agent/docs/session-format.md`, version 0.84.1),
verified against the package's shipped `.d.ts` files — not from a live corpus. See
docs/superpowers/specs/2026-09-25-pi-analyzer-design.md Sec 1 for why, and Task 7 of the plan
that implements this for the real-session validation step this gap requires.

Entries are consumed in file order (a tree's flattened write order), not walked from the active
leaf backward — see the spec Sec 3.4 for why, and what a leaf-only mode would need.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sessionkit.parse import FileOp, Message, ParsedSession, SysEvent, ToolCall, digest
from sessionkit.redact import preview

MSG_PREVIEW = 200
INPUT_PREVIEW = 2000
OUTPUT_PREVIEW = 2000

#: pi's builtin file tools use lowercase names and a single `path` argument key — unlike
#: Claude Code's capitalised `Read`/`Edit`/`MultiEdit`/`NotebookEdit` with `file_path`.
FILE_TOOLS = {"read": "read", "write": "write", "edit": "edit"}
PATH_KEY = "path"


def _text_of(content: Any) -> str:
    """Flatten a `string | (TextContent|ImageContent|ThinkingContent)[]` field to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            parts.append(str(block.get("text") or block.get("thinking") or ""))
    return "\n".join(p for p in parts if p)


def _delta_ms(start: str, end: str) -> int | None:
    """Milliseconds between two ISO-8601 timestamps, or ``None`` if either is unusable."""
    from datetime import datetime

    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((b - a).total_seconds() * 1000)


class _Parser:
    """Accumulates one pi session file's entries into a ParsedSession."""

    def __init__(self, path: Path, source_id: str, sid: str,
                session: ParsedSession | None = None) -> None:
        self.out = session if session is not None else ParsedSession(
            sid=sid, source_id=source_id, path=str(path))
        self.pending: dict[str, ToolCall] = {}

    def _touch_ts(self, ts: str) -> None:
        out = self.out
        if not ts:
            return
        if not out.started_at or ts < out.started_at:
            out.started_at = ts
        if ts > out.ended_at:
            out.ended_at = ts

    def feed(self, line_no: int, rec: dict[str, Any]) -> None:
        """Dispatch one decoded top-level entry by its ``type``."""
        etype = rec.get("type")
        if etype == "session":
            self._header(rec)
        elif etype == "message":
            self._message(line_no, rec)
        elif etype in ("model_change", "thinking_level_change", "label", "session_info"):
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(SysEvent(line_no, ts, etype))
        elif etype == "compaction":
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(
                SysEvent(line_no, ts, "compaction", str(rec.get("tokensBefore") or "")))
        elif etype == "branch_summary":
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(SysEvent(line_no, ts, "branch_summary"))
        elif etype in ("custom", "custom_message"):
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            custom_type = str(rec.get("customType") or "")
            self.out.sysev.append(SysEvent(line_no, ts, f"custom:{custom_type}"))
        else:
            self.out.malformed_lines += 1

    def _header(self, rec: dict[str, Any]) -> None:
        out = self.out
        out.cwd = str(rec.get("cwd") or out.cwd)
        self._touch_ts(str(rec.get("timestamp") or ""))

    def _message(self, line_no: int, rec: dict[str, Any]) -> None:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            self.out.malformed_lines += 1
            return
        role = msg.get("role")
        ts = str(rec.get("timestamp") or "")
        self._touch_ts(ts)
        if role == "user":
            self._user(line_no, ts, msg)
        elif role == "assistant":
            self._assistant(line_no, ts, msg)
        elif role == "toolResult":
            self._tool_result(line_no, ts, msg)
        elif role == "bashExecution":
            self._bash(line_no, ts, msg)
        # custom / branchSummary / compactionSummary message roles carry no reportable field
        # beyond what their own custom_message/branch_summary/compaction entry already recorded.

    def _user(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        text = _text_of(msg.get("content"))
        out = self.out
        if text.strip():
            out.turns += 1
            if not out.first_prompt:
                out.first_prompt = preview(text, MSG_PREVIEW)
        out.messages.append(Message(uuid=f"u-{line_no}", line=line_no, ts=ts, role="user",
                                    text_len=len(text), preview=preview(text, MSG_PREVIEW)))

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
        text = _text_of(content)
        out.messages.append(Message(uuid=f"a-{line_no}", line=line_no, ts=ts, role="assistant",
                                    model=model, tok_in=tok_in, tok_out=tok_out, tok_cr=tok_cr,
                                    tok_cc=tok_cc, text_len=len(text),
                                    preview=preview(text, MSG_PREVIEW)))
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._open_tool_call(line_no, ts, block)

    def _open_tool_call(self, line_no: int, ts: str, block: dict[str, Any]) -> None:
        raw_input = block.get("arguments")
        call = ToolCall(tool_use_id=str(block.get("id") or ""), line=line_no,
                        name=str(block.get("name") or "?"), ts=ts,
                        input_digest=digest(raw_input),
                        input_preview=preview(json.dumps(raw_input, default=str), INPUT_PREVIEW))
        self.out.tools.append(call)
        if call.tool_use_id:
            self.pending[call.tool_use_id] = call
        self._file_op(call, raw_input, ts)

    def _file_op(self, call: ToolCall, raw_input: Any, ts: str) -> None:
        op = FILE_TOOLS.get(call.name)
        if not op or not isinstance(raw_input, dict):
            return
        path = raw_input.get(PATH_KEY)
        if isinstance(path, str) and path:
            self.out.files.append(FileOp(path, op, ts, call.tool_use_id, call.line))

    def _tool_result(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        call = self.pending.pop(str(msg.get("toolCallId") or ""), None)
        if call is None:
            return
        body = _text_of(msg.get("content"))
        call.result_ts = ts
        call.result_line = line_no
        call.dur_ms = _delta_ms(call.ts, ts)
        call.is_error = bool(msg.get("isError"))
        call.out_bytes = len(body)
        call.output_preview = preview(body, OUTPUT_PREVIEW)

    def _bash(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        exit_code = msg.get("exitCode")
        cancelled = bool(msg.get("cancelled"))
        is_error = cancelled or (exit_code is not None and exit_code != 0)
        output = str(msg.get("output") or "")
        call = ToolCall(tool_use_id=f"bash-{line_no}", line=line_no, name="bash", ts=ts,
                        result_ts=ts, result_line=line_no, is_error=is_error,
                        out_bytes=len(output), output_preview=preview(output, OUTPUT_PREVIEW),
                        input_preview=preview(str(msg.get("command") or ""), INPUT_PREVIEW),
                        input_digest=digest(msg.get("command")))
        self.out.tools.append(call)

    def finish(self) -> ParsedSession:
        for call in self.pending.values():
            call.err_class = "no-result"
        return self.out


def parse_file(path: Path, source_id: str) -> ParsedSession:
    """Parse one pi session JSONL file into a normalised session.

    Malformed or unrecognised lines are counted rather than fatal, matching
    :func:`sessionkit.parse.parse_file`'s behaviour for the same reason: a partially written
    session for a live ``pi`` run is normal, not corrupt.

    Args:
        path: Path to the session ``.jsonl`` file.
        source_id: Owning source identifier.

    Returns:
        The parsed session; ``sid`` falls back to the filename stem if no header line carried
        an ``id``.
    """
    parser = _Parser(path, source_id, path.stem)
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return parser.finish()
    with handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                parser.out.malformed_lines += 1
                continue
            if not isinstance(rec, dict):
                parser.out.malformed_lines += 1
                continue
            if line_no == 1 and rec.get("type") == "session" and rec.get("id"):
                parser.out.sid = str(rec["id"])
            parser.feed(line_no, rec)
    return parser.finish()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse_pi -v`
Expected: PASS (all tests)

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add sessionkit/parse_pi.py tests/fixtures_pi.py tests/test_parse_pi.py
git commit -m "Add pi session parser"
```

---

### Task 3: Subagent nesting

**Files:**
- Modify: `sessionkit/parse.py` (one new field on `ParsedSession`)
- Modify: `sessionkit/parse_pi.py`
- Modify: `tests/fixtures_pi.py`
- Modify: `tests/test_parse_pi.py`

**Interfaces:**
- Consumes: `_Parser.__init__(path, source_id, sid, session=None)` from Task 2 (already accepts
  a pre-built session, needed here to build a child in place).
- Produces: `ParsedSession.child_sessions: list[ParsedSession]` (consumed by Task 4);
  `ParsedSession.dispatch_edges` entries of the form `(tool_call_id, child_sid)` (already an
  existing field — reused, not new).

When a `toolCall` named `"subagent"` closes with a `toolResult` carrying `details.results`
(the example subagent extension's shape — spec Sec 4), each result becomes a synthetic child
session built from that result's own flat `messages[]` list, which has no entry wrapper — those
messages are raw pi-ai `Message` objects with a Unix-ms `timestamp`, not the ISO string Task 2
uses. A missing or empty `details`/`results` must produce zero children without raising, since
that shape only appears for one specific extension and every other tool named anything else, or
a `subagent` call that failed before producing a result, must be unaffected.

- [ ] **Step 1: Add the fixture helper for a subagent result**

Append to `tests/fixtures_pi.py`:

```python
def subagent_result(agent: str, task: str, output_text: str, exit_code: int = 0,
                    stop_reason: str = "stop", error_message: str = "",
                    usage_cost: float = 0.01) -> dict[str, Any]:
    """One entry of a `subagent` toolResult's `details.results[]` array."""
    return {
        "agent": agent, "agentSource": "user", "task": task, "exitCode": exit_code,
        "stopReason": stop_reason, "errorMessage": error_message,
        "usage": {"input": 200, "output": 100, "cacheRead": 0, "cacheWrite": 0,
                 "cost": usage_cost},
        "messages": [
            {"role": "user", "content": task, "timestamp": 1735689600000},
            {"role": "assistant", "content": [{"type": "text", "text": output_text}],
             "provider": "anthropic", "model": "claude-haiku-4-5", "stopReason": stop_reason,
             "timestamp": 1735689601000},
        ],
    }
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_parse_pi.py`:

```python
class ParsePiSubagentTest(unittest.TestCase):
    """Nested subagent tool-result expansion into synthetic child sessions — spec Sec 4."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parse(self, entries: list[dict], **kw) -> "object":
        return parse_file(fx.write(self.tmp, entries, **kw), "pi")

    def test_subagent_result_produces_synthetic_children(self) -> None:
        details = {"mode": "parallel", "results": [
            fx.subagent_result("scout", "find the bug", "found it in foo.py"),
            fx.subagent_result("worker", "fix it", "fixed", exit_code=1, stop_reason="error",
                               error_message="ran out of time"),
        ]}
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "delegate this"),
            fx.assistant_msg("e2", "e1",
                             [fx.tool_call_block("c1", "subagent", {"tasks": [{}, {}]})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "Parallel: 1/2 succeeded",
                               details=details),
        ])
        self.assertEqual(len(session.child_sessions), 2)
        scout, worker = session.child_sessions
        self.assertEqual(scout.sid, f"{session.sid}:c1:0")
        self.assertTrue(scout.is_subagent)
        self.assertEqual(scout.parent_sid, session.sid)
        self.assertEqual(scout.agent_type, "scout")
        self.assertEqual(scout.cwd, session.cwd)
        self.assertEqual(scout.end_state, "complete")
        self.assertEqual(worker.end_state, "error-cascade")
        self.assertEqual(worker.end_reason, "ran out of time")
        self.assertEqual(session.dispatch_edges, [("c1", scout.sid), ("c1", worker.sid)])

    def test_subagent_child_carries_its_own_messages_and_usage(self) -> None:
        details = {"mode": "single", "results": [
            fx.subagent_result("scout", "find the bug", "found it in foo.py"),
        ]}
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "delegate this"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                "c1", "subagent", {"agent": "scout", "task": "find the bug"})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "found it in foo.py",
                               details=details),
        ])
        child = session.child_sessions[0]
        self.assertEqual([m.role for m in child.messages], ["user", "assistant"])
        self.assertEqual(child.tok_in, 200)
        self.assertEqual(child.tok_out, 100)
        self.assertAlmostEqual(child.cost_usd, 0.01)

    # --- Review Focus: subagent toolResult without details/results ---------------------
    def test_tool_result_named_subagent_without_details_produces_no_children(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                "c1", "subagent", {"agent": "x", "task": "y"})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "Canceled: not approved."),
        ])
        self.assertEqual(session.child_sessions, [])
        self.assertEqual(session.dispatch_edges, [])
        self.assertFalse(session.tools[0].is_error)

    def test_tool_result_named_subagent_with_empty_results_produces_no_children(self) -> None:
        session = self.parse([
            fx.header(),
            fx.user_msg("e1", None, "go"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block("c1", "subagent", {})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "Invalid parameters.",
                               details={"mode": "single", "results": []}),
        ])
        self.assertEqual(session.child_sessions, [])
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse_pi.ParsePiSubagentTest -v`
Expected: FAIL — `AttributeError: 'ParsedSession' object has no attribute 'child_sessions'`

- [ ] **Step 4: Add `child_sessions` to `ParsedSession`**

In `sessionkit/parse.py`, in the `ParsedSession` dataclass, add the field next to
`dispatch_edges` (both describe dispatch relationships, so keeping them adjacent matches how
the class already groups related fields):

```python
    dispatch_edges: list[tuple[str, str]] = field(default_factory=list)
    #: Synthetic subagent-dispatch child sessions extracted from a `subagent` tool call's
    #: nested `details.results[].messages[]` — populated only by parse_pi.py; always empty
    #: for Claude Code / opencode sessions. See parse_pi.py's `_subagent_children`.
    child_sessions: list["ParsedSession"] = field(default_factory=list)
```

- [ ] **Step 5: Add `_ms_to_iso` and `_subagent_children` to `parse_pi.py`**

Add near the top of `sessionkit/parse_pi.py`, after `_delta_ms`:

```python
def _ms_to_iso(ms: Any) -> str:
    """Convert a pi-ai `Message.timestamp` (Unix ms) to the ISO string every other `ts` field
    in this module uses. Nested subagent messages carry this instead of an entry wrapper."""
    from datetime import datetime, timezone

    if not isinstance(ms, (int, float)):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")
```

Add after the `_Parser` class:

```python
def _subagent_children(parent_sid: str, source_id: str, path: str, parent_cwd: str,
                       call: ToolCall, details: dict[str, Any]) -> list[ParsedSession]:
    """Expand one closed `subagent` tool call's nested `details.results[]` into synthetic
    child ParsedSessions (spec Sec 4).

    Returns ``[]`` if `details` carries no results — covers both a non-subagent tool that
    happens to be named "subagent" in some other extension and a subagent call that failed
    before producing any result.
    """
    results = details.get("results") if isinstance(details, dict) else None
    if not isinstance(results, list) or not results:
        return []

    children: list[ParsedSession] = []
    for i, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        child_sid = f"{parent_sid}:{call.tool_use_id}:{i}"
        child = ParsedSession(sid=child_sid, source_id=source_id, path=path, cwd=parent_cwd,
                              is_subagent=True, parent_sid=parent_sid,
                              agent_type=str(result.get("agent") or ""))
        sub = _Parser(Path(path), source_id, child_sid, session=child)
        for msg in result.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            ts = _ms_to_iso(msg.get("timestamp"))
            if role == "user":
                sub._user(0, ts, msg)
            elif role == "assistant":
                sub._assistant(0, ts, msg)
            elif role == "toolResult":
                sub._tool_result(0, ts, msg)
            elif role == "bashExecution":
                sub._bash(0, ts, msg)
        sub.finish()

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        child.tok_in = int(usage.get("input") or 0)
        child.tok_out = int(usage.get("output") or 0)
        child.tok_cache_read = int(usage.get("cacheRead") or 0)
        child.tok_cache_create = int(usage.get("cacheWrite") or 0)
        child.cost_usd = float(usage.get("cost") or 0.0)

        exit_code = result.get("exitCode")
        stop_reason = result.get("stopReason")
        if exit_code == 0 and stop_reason not in ("error", "aborted"):
            child.end_state, child.end_reason = "complete", ""
        else:
            child.end_state = "error-cascade"
            child.end_reason = str(result.get("errorMessage") or result.get("stderr") or "")

        children.append(child)
    return children
```

- [ ] **Step 6: Call it from `_tool_result`**

In `sessionkit/parse_pi.py`, replace the `_tool_result` method with:

```python
    def _tool_result(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        call = self.pending.pop(str(msg.get("toolCallId") or ""), None)
        if call is None:
            return
        body = _text_of(msg.get("content"))
        call.result_ts = ts
        call.result_line = line_no
        call.dur_ms = _delta_ms(call.ts, ts)
        call.is_error = bool(msg.get("isError"))
        call.out_bytes = len(body)
        call.output_preview = preview(body, OUTPUT_PREVIEW)
        if call.name == "subagent":
            details = msg.get("details")
            children = _subagent_children(
                self.out.sid, self.out.source_id, self.out.path, self.out.cwd,
                call, details if isinstance(details, dict) else {})
            self.out.child_sessions.extend(children)
            self.out.dispatch_edges.extend((call.tool_use_id, c.sid) for c in children)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse_pi -v`
Expected: PASS (all tests, both classes)

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: PASS — in particular `tests/test_parse.py` and `tests/test_corpus.py`, since
`ParsedSession` gained a field: confirm no Claude Code test asserts an exact field count or
uses `dataclasses.fields()`/`asdict()` in a way a new default-valued field would break.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/parse.py sessionkit/parse_pi.py tests/fixtures_pi.py tests/test_parse_pi.py
git commit -m "Expand pi subagent tool results into synthetic child sessions"
```

---

### Task 4: Wire pi into corpus.py

**Files:**
- Modify: `sessionkit/corpus.py`
- Test: `tests/test_corpus_pi.py` (new)

**Interfaces:**
- Consumes: `sources.Source.sessions_root` (Task 1); `parse_pi.parse_file` (Task 2);
  `ParsedSession.child_sessions` (Task 3); existing `classify.{annotate_errors,
  derive_end_state, detect}`, `parse.project_key`.
- Produces: `corpus.load_one(source, path, dir_name) -> list[Loaded]` — **signature change**
  from the current `-> Loaded`. `corpus.transcripts()` now also handles `kind == "pi"`.

`load_one` changes shape because a pi transcript can now yield more than one `Loaded` (the
parent plus any synthetic subagent children from Task 3) — every caller must be updated in the
same change, which is why this is one task rather than two: `load()` and `load_session()` are
each individually broken by a partial application of this signature change.

- [ ] **Step 1: Write the failing tests**

```python
"""End-to-end tests: a synthetic pi corpus on disk, parsed through to reports."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionkit import corpus, query
from tests import fixtures_pi as fx


class PiCorpusTest(unittest.TestCase):
    """Parse a controlled pi corpus and query it, same shape as tests/test_corpus.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pi_home = self.tmp / "pi-agent"
        self.pi_home.mkdir()
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.tmp / "claude"),  # left absent: unreachable, on purpose
            "PI_DIR": str(self.pi_home),
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_finds_pi_session(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        loaded = corpus.load()
        self.assertEqual(len(loaded.sessions), 1)
        self.assertEqual(loaded.sessions[0].session.sid, fx.SID)
        self.assertEqual(loaded.sessions[0].session.source_id, "pi")
        self.assertEqual(loaded.failed, 0)

    def test_doctor_reports_pi_source(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        loaded = corpus.load()
        rows = {r["id"]: r for r in query.source_rows(loaded)}
        self.assertIn("pi", rows)
        self.assertTrue(rows["pi"]["reachable"])
        self.assertEqual(rows["pi"]["kind"], "pi")

    def test_source_filter_isolates_pi_sessions(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        loaded = corpus.load()
        f = query.Filter(source="pi")
        matched = [e for e in loaded.sessions if f.matches(e)]
        self.assertEqual(len(matched), 1)

    def test_load_session_finds_pi_session_by_id_prefix(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        entry = corpus.load_session(fx.SID[:8])
        self.assertIsNotNone(entry)
        self.assertEqual(entry.session.sid, fx.SID)

    def test_subagent_children_are_added_to_the_corpus(self) -> None:
        details = {"mode": "single", "results": [
            fx.subagent_result("scout", "find the bug", "found it in foo.py"),
        ]}
        fx.write(self.pi_home, [
            fx.header(),
            fx.user_msg("e1", None, "delegate this"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                "c1", "subagent", {"agent": "scout", "task": "find the bug"})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "found it in foo.py",
                               details=details),
        ])
        loaded = corpus.load()
        self.assertEqual(len(loaded.sessions), 2, "parent + one synthetic subagent child")
        sids = {e.session.sid for e in loaded.sessions}
        self.assertIn(f"{fx.SID}:c1:0", sids)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus_pi -v`
Expected: FAIL — `corpus.load()` finds nothing (`transcripts()` still only globs
`projects/*/*.jsonl`, and the pi source's `kind` is ignored).

- [ ] **Step 3: Import `parse_pi` and branch `transcripts()`**

In `sessionkit/corpus.py`, add the import alongside the existing `parse` import:

```python
from sessionkit import parse_pi
```

Replace `transcripts()` with:

```python
def transcripts(source: src.Source) -> list[tuple[Path, str]]:
    """Every session transcript under a source, paired with its project directory name.

    Claude Code / opencode use `projects/<project>/<session>.jsonl`, with subagent transcripts
    one level deeper at `projects/<project>/<parent-session>/subagents/agent-*.jsonl`. pi has
    no `projects/` indirection at all: `sessions/<project>/<file>.jsonl` directly, and no
    separate subagent glob — nested subagent conversations are inline (see parse_pi.py).
    """
    if source.kind == "pi":
        return _pi_transcripts(source)

    found: list[tuple[Path, str]] = []
    root = source.projects_dir
    if not root.is_dir():
        return found
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for project_dir in entries:
        try:
            files = sorted(project_dir.glob("*.jsonl"))
            files.extend(sorted(project_dir.glob("*/subagents/*.jsonl")))
        except OSError:
            continue
        found.extend((f, project_dir.name) for f in files)
    return found


def _pi_transcripts(source: src.Source) -> list[tuple[Path, str]]:
    """Every ``.jsonl`` session under a pi source, paired with its project directory name."""
    found: list[tuple[Path, str]] = []
    root = source.sessions_root
    if not root.is_dir():
        return found
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for project_dir in entries:
        try:
            files = sorted(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        found.extend((f, project_dir.name) for f in files)
    return found
```

- [ ] **Step 4: Change `load_one` to return a list**

Replace `load_one` with:

```python
def load_one(source: src.Source, path: Path, dir_name: str) -> list[Loaded]:
    """Parse one transcript and derive everything the reports need from it.

    Returns one `Loaded` for a Claude Code/opencode transcript, or one `Loaded` for a pi
    session plus one more per synthetic subagent child its `subagent` tool calls produced
    (parse_pi.py, spec Sec 4) — always at least one entry.
    """
    if source.kind == "pi":
        parent = parse_pi.parse_file(path, source.id)
        annotate_errors(parent)
        parent.end_state, parent.end_reason = derive_end_state(parent)
        loaded = [Loaded(parent, project_key(parent.cwd, source.id, dir_name),
                         detect(parent))]
        for child in parent.child_sessions:
            annotate_errors(child)
            # end_state/end_reason were already assigned in parse_pi from the subagent
            # result's exitCode/stopReason (spec Sec 4) — derive_end_state assumes a live
            # tail of tool calls a synthetic child, built from an already-finished
            # subprocess, does not have.
            loaded.append(Loaded(child, project_key(child.cwd, source.id, dir_name),
                                 detect(child)))
        return loaded

    session = parse.parse_file(path, source.id)
    if source.location == "container":
        session.cwd = src.rewrite_cwd(session.cwd, source.origin)
    annotate_errors(session)
    session.end_state, session.end_reason = derive_end_state(session)
    anomalies = detect(session)
    return [Loaded(session, project_key(session.cwd, source.id, dir_name), anomalies)]
```

- [ ] **Step 5: Update `load()`**

```python
def load(only: str | None = None) -> Corpus:
    """Discover every reachable source and parse its transcripts.

    Args:
        only: Restrict to a single source id.

    Returns:
        The parsed corpus, including the sources that could not be read so a report can say
        what it did not cover.
    """
    found = src.discover()
    corpus = Corpus(sources=found)
    for source in src.scannable(found):
        if only and source.id != only:
            continue
        for path, dir_name in transcripts(source):
            try:
                corpus.sessions.extend(load_one(source, path, dir_name))
            except OSError:
                corpus.failed += 1
    _disambiguate(corpus.sessions)
    return corpus
```

- [ ] **Step 6: Update `load_session()`**

```python
def load_session(prefix: str) -> Loaded | None:
    """Parse only the transcripts whose *filename* could hold this session id.

    pi's filenames (`<timestamp>_<uuid>.jsonl`) don't share a prefix with either the header's
    session id or a synthetic child's `<sid>:<call_id>:<i>` id, so the filename pre-filter that
    speeds this up for Claude Code is skipped for pi sources — every pi file is parsed and
    checked. Acceptable for now (spec Sec 9); revisit if `sk show` proves slow on a large pi
    corpus.
    """
    needle = prefix.lower()
    found: list[Loaded] = []
    for source in src.scannable(src.discover()):
        for path, dir_name in transcripts(source):
            if source.kind != "pi":
                stem = path.stem
                candidate = stem[6:] if stem.startswith("agent-") else stem
                if not candidate.lower().startswith(needle):
                    continue
            try:
                entries = load_one(source, path, dir_name)
            except OSError:
                continue
            for entry in entries:
                if entry.session.sid.lower().startswith(needle):
                    found.append(entry)
    if not found:
        return None
    return max(found, key=lambda e: e.session.ended_at or "")
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus_pi -v`
Expected: PASS (all 5 tests)

- [ ] **Step 8: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: PASS — `tests/test_corpus.py`'s existing tests call `corpus.load()`/`load_session()`
too; confirm they still pass with the new list-returning `load_one`.

- [ ] **Step 9: Commit**

```bash
git add sessionkit/corpus.py tests/test_corpus_pi.py
git commit -m "Parse pi sessions through the shared corpus pipeline"
```

---

### Task 5: Recognize pi's subagent dispatch tool name

**Files:**
- Modify: `sessionkit/classify.py:337-338`
- Modify: `sessionkit/query.py:765-766`
- Modify: `tests/test_classify.py`
- Modify: `tests/test_corpus_pi.py`

**Interfaces:**
- Consumes: `ToolCall.name` (existing).
- Produces: no new public interface — widens two existing behaviors to also match `"subagent"`.

Both `classify._agent_trouble` (orphaned-dispatch anomaly detection) and
`query.children_rows` (the `sk children` join) hardcode the literal Claude Code dispatch tool
name `"Agent"`. Task 4 already makes pi's `dispatch_edges`/`is_subagent` data shape-compatible;
without this task, `sk children` on a pi session with a `subagent` tool call reports zero
dispatches, and an orphaned pi subagent dispatch (no result at all) is invisible to the
`orphan-subagent` anomaly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_classify.py` (matching the existing `_call`/`_session` helpers already
in that file):

```python
    def test_orphan_pi_subagent_dispatch_detected(self) -> None:
        session = _session(tools=[_call(1, name="subagent", err_class="no-result")])
        anomalies = detect(session)
        self.assertTrue(any(a.kind == "orphan-subagent" for a in anomalies))

    def test_orphan_claude_code_agent_dispatch_still_detected(self) -> None:
        # Regression: widening the check must not stop matching the original "Agent" name.
        session = _session(tools=[_call(1, name="Agent", err_class="no-result")])
        anomalies = detect(session)
        self.assertTrue(any(a.kind == "orphan-subagent" for a in anomalies))
```

Append to `tests/test_corpus_pi.py`'s `PiCorpusTest` class:

```python
    def test_subagent_dispatch_resolves_through_children_rows(self) -> None:
        details = {"mode": "single", "results": [
            fx.subagent_result("scout", "find the bug", "found it in foo.py"),
        ]}
        fx.write(self.pi_home, [
            fx.header(),
            fx.user_msg("e1", None, "delegate this"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                "c1", "subagent", {"agent": "scout", "task": "find the bug"})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "found it in foo.py",
                               details=details),
        ])
        loaded = corpus.load()
        parent = query.find_session(loaded, fx.SID)
        rows = query.children_rows(parent, loaded)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["resolved"])
        self.assertEqual(rows[0]["agent_type"], "scout")
        self.assertEqual(rows[0]["state"], "complete")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_classify tests.test_corpus_pi -v`
Expected: FAIL — `test_orphan_pi_subagent_dispatch_detected` and
`test_subagent_dispatch_resolves_through_children_rows` fail (`Agent`-only checks don't match
`subagent`); `test_orphan_claude_code_agent_dispatch_still_detected` already passes (recorded
here as the regression guard, not a new failure).

- [ ] **Step 3: Widen `classify._agent_trouble`**

In `sessionkit/classify.py`:

```python
    orphans = [c.line for c in session.tools
               if c.name in ("Agent", "subagent") and c.err_class == "no-result"]
```

- [ ] **Step 4: Widen `query.children_rows`**

In `sessionkit/query.py`:

```python
    for tool in entry.session.tools:
        if tool.name not in ("Agent", "subagent"):
            continue
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_classify tests.test_corpus_pi -v`
Expected: PASS (all tests)

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add sessionkit/classify.py sessionkit/query.py tests/test_classify.py tests/test_corpus_pi.py
git commit -m "Recognize pi's subagent tool as an agent dispatch"
```

---

### Task 6: Reword skills to be provider-neutral

**Files:**
- Modify: `skills/error-patterns/SKILL.md`
- Modify: `skills/session-forensics/SKILL.md`

**Interfaces:** None — documentation only, no code path changes. (`skills/unfinished-work`
was checked and contains no Claude-Code-specific wording; it needs no change.)

Both skills already work fleet-wide once `sk` covers pi (Tasks 1-5) — they only ever call `sk`,
never read transcripts directly. This task fixes the two places their prose still says
"Claude Code" when they mean "every reachable session."

- [ ] **Step 1: Reword `skills/error-patterns/SKILL.md`**

Change the frontmatter `description` (currently):

```
description: Find recurring tool failures across ALL Claude Code sessions on this machine and propose concrete fixes (CLAUDE.md rules, hook changes, missing tools). Use when asked why sessions keep failing, what keeps breaking, which errors repeat, or for a periodic health check of the session fleet. Triggers on "what keeps failing", "recurring errors", "why do my sessions error", "error patterns", "session health check". NOT for reviewing a single session's failure — use session-forensics for that.
```

to:

```
description: Find recurring tool failures across ALL coding-agent sessions on this machine (Claude Code, pi, and any other reachable source) and propose concrete fixes (CLAUDE.md rules, hook changes, missing tools). Use when asked why sessions keep failing, what keeps breaking, which errors repeat, or for a periodic health check of the session fleet. Triggers on "what keeps failing", "recurring errors", "why do my sessions error", "error patterns", "session health check". NOT for reviewing a single session's failure — use session-forensics for that.
```

Change the body line (currently):

```
Cluster every failed tool call across every reachable Claude Code session and turn the top
clusters into fixes.
```

to:

```
Cluster every failed tool call across every reachable coding-agent session — Claude Code, pi,
and any other source `sk` can see — and turn the top clusters into fixes.
```

- [ ] **Step 2: Reword `skills/session-forensics/SKILL.md`**

Change the frontmatter `description` (currently):

```
description: Diagnose why ONE Claude Code session went wrong — looped, stalled, killed a subagent, or ended interrupted — and what actually ran during it. Use when asked why a specific session failed, looped, stalled, or produced a bad result, or to review what commands/tools an agent (including a subagent) actually invoked. Triggers on "why did this session fail", "why did it loop", "what did that agent actually run", "review this session". NOT for a fleet-wide question about what keeps failing across many sessions — use error-patterns for that.
```

to:

```
description: Diagnose why ONE coding-agent session (Claude Code, pi, or any other reachable source) went wrong — looped, stalled, killed a subagent, or ended interrupted — and what actually ran during it. Use when asked why a specific session failed, looped, stalled, or produced a bad result, or to review what commands/tools an agent (including a subagent) actually invoked. Triggers on "why did this session fail", "why did it loop", "what did that agent actually run", "review this session". NOT for a fleet-wide question about what keeps failing across many sessions — use error-patterns for that.
```

Change the body line (currently):

```
Diagnose a single Claude Code session (or subagent) using `sk forensics` and `sk commands`. This
is a **single-session, read-only** analysis — it names what went wrong, cites the exact lines,
and ends with a prevention hint, never a rewritten narrative of the transcript.
```

to:

```
Diagnose a single coding-agent session (or subagent) — from Claude Code, pi, or any other
reachable source — using `sk forensics` and `sk commands`. This is a **single-session,
read-only** analysis — it names what went wrong, cites the exact lines, and ends with a
prevention hint, never a rewritten narrative of the transcript.
```

- [ ] **Step 3: Verify no stray "Claude Code"-only wording remains**

Run: `grep -n "Claude Code" skills/error-patterns/SKILL.md skills/session-forensics/SKILL.md`
Expected: no output (both files' only two occurrences each were just replaced). If any line
still reads as Claude-Code-exclusive rather than descriptive (e.g. a `CLAUDE.md`-rule
recommendation, which is correctly Claude-Code-specific and must stay), leave it — this task
only removes wording that falsely narrows *scope*, not Claude-Code-specific *fix
recommendations*, which remain accurate advice for Claude Code sessions found by the same
fleet-wide search.

- [ ] **Step 4: Commit**

```bash
git add skills/error-patterns/SKILL.md skills/session-forensics/SKILL.md
git commit -m "Reword error-patterns and session-forensics as provider-neutral"
```

---

### Task 7: Validate against a real pi session

**Files:** None created or modified unless real output reveals a bug — this task is a
validation gate, closing the acceptance gap the spec calls out in Sec 1 and Sec 8: every
prior task was built from pi's documented schema, never from a session `pi` itself produced.

**Interfaces:** None new.

- [ ] **Step 1: Produce a real pi session**

Run a short, real `pi` invocation that exercises a tool call (session-saving is pi's default;
only `--no-session` disables it):

```bash
pi -p "List the files in the current directory using your read/bash tools, then say done."
```

- [ ] **Step 2: Locate the resulting session file**

```bash
find ~/.pi/agent/sessions -name '*.jsonl' -newer /tmp -print
```

(Or use the most recently modified file under `~/.pi/agent/sessions/`.)

- [ ] **Step 3: Parse it directly and inspect for surprises**

```bash
cd /home/david/orca/workspaces/claude-session-analyzer/halfbeak
PYTHONPATH=. python3 -c "
from pathlib import Path
from sessionkit.parse_pi import parse_file
p = parse_file(Path('<the file found in Step 2>'), 'pi')
print('malformed_lines:', p.malformed_lines)
print('turns:', p.turns, 'tools:', len(p.tools), 'messages:', len(p.messages))
print('tok_in/out:', p.tok_in, p.tok_out, 'cost:', p.cost_usd)
for t in p.tools:
    print(' -', t.name, 'error' if t.is_error else 'ok', t.err_class)
"
```

Expected: `malformed_lines: 0`, and the tool list matches what the transcript actually shows in
the terminal (the `read`/`bash`/`ls`/etc. calls pi made). `tok_in`/`tok_out`/`cost` should be
positive and roughly plausible for a short exchange.

- [ ] **Step 4: Run it through the full `sk` CLI**

```bash
PI_DIR=~/.pi/agent PYTHONPATH=. python3 -m sessionkit index --source pi --since 1d
PI_DIR=~/.pi/agent PYTHONPATH=. python3 -m sessionkit show <sid-from-index> --mode timeline
PI_DIR=~/.pi/agent PYTHONPATH=. python3 -m sessionkit doctor
```

Expected: the session appears in `index`, `show --mode timeline` renders its turns without a
traceback, and `doctor`'s Sources table lists `pi` as reachable with a nonzero session count
reflected in the Corpus table.

- [ ] **Step 5: Fold any real-schema surprises back into the spec and fixtures**

If Steps 1-4 surfaced anything the spec or Tasks 1-6 got wrong (a field name, a missing case, an
unexpected `null`), fix `sessionkit/parse_pi.py` and add a fixture in `tests/fixtures_pi.py` /
test in `tests/test_parse_pi.py` that would have caught it, then re-run Step 3. Do not consider
this task done while a known discrepancy between the real file and the parser remains
unaddressed — that is exactly the gap this task exists to close.

If nothing surfaced, note that plainly rather than inventing a finding: the acceptance
criterion is that this was actually checked, not that a bug was found.

- [ ] **Step 6: Commit any fixes**

```bash
git add sessionkit/parse_pi.py tests/fixtures_pi.py tests/test_parse_pi.py
git commit -m "Fix pi parser against a real session file"
```

(Skip this commit if Step 5 found nothing to fix.)
