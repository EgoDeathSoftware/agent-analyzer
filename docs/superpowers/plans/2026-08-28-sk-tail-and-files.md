# sk tail + sk files (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Phase 3 of the sessionkit roadmap — a `sk tail` command that returns the last N
turns of a session (session-scoped or corpus-wide over non-`complete` sessions) with a
deterministic `tail_signal` classifier, a `sk files` command that reports which files a session
touched with an optional `--uncommitted` git-status intersection, and a new `unfinished-work`
skill that composes both into "what is at risk of being lost." Retires the planned `sk
unfinished` name and `gitlink.py` module from `PLAN.md`.

**Architecture:** Two new commands added through the existing `cli.py` subparser wiring
(`common`/`scoped` parent parsers), backed by pure aggregation in `query.py`. `tail_signal`
extraction lives in `classify.py` alongside the error taxonomy and `derive_end_state`, driven by
a small I/O wrapper in `query.py` that re-reads full message text via `parse.read_line` (the
existing 200-char `preview` misses late-in-message completion markers). `--uncommitted`
inlines a `git status --porcelain=v1 -z` subprocess against the session's `cwd` — no
`gitlink.py` module; one caller, one command. Live-session exclusion in `--all` mode reads
`<claude_root>/sessions/*.json` through a small helper in `sources.py`. The `unfinished-work`
skill lives in `skills/unfinished-work/SKILL.md`, following the same shape as
`skills/session-forensics/SKILL.md` (front-matter, boundaries table, `$SK` resolver, procedure).

**Tech Stack:** Python 3.12 stdlib only. `unittest` with inline fixtures. `subprocess.run`
for git (fixed argv, `check=False`, timeout, `env={}` overrides to strip inherited `GIT_*`).

**Spec:** `PLAN.md` (this plan implements §7 Phase 3, with the `sk unfinished` → `sk tail`
rename and `gitlink.py` drop agreed in-chat 2026-08-28). Cross-references: `PLAN.md` §2 (the
CLI/skill split), §3.1 (subagent transcript facts), §3.2.2 (`sessions/<pid>.json` live
registry), §4 (three-layer output contract, cell-truncation rules), §5.1/§5.2 (command and
skill lists that get amended), §5.3 (handle resolution deferred to its own pass — this plan
keeps `<sid>` required, matching `sk show`/`forensics`/`commands`), §9 (read-only CLI).

## Global Constraints

- Runtime and tests are **stdlib-only** — no new dependencies (`PLAN.md` §3).
- Tests run via `PYTHONPATH=. python3 -m unittest discover -s tests -t .` (`PLAN.md` §8).
  Every new classifier value and detector needs a **positive and a negative fixture case**.
- `sk` is **read-only** — no new subprocess call mutates the tree, index, config, or refs;
  no path outside `stdout` is written (`PLAN.md` §9).
- **No silent truncation of rows; `--json` never truncates a cell** (`SPEC.md` §4). New cells
  respect `render._truncate_cell`; a dropped row emits the standard `raise --budget-kb`
  omitted-count line.
- Global flags (`--json`, `--budget-kb`) must keep parsing before **or** after the subcommand
  (`cli.py`'s `common`/`scoped` parent-parser pattern) — new subcommands follow the existing
  `parents=[common, scoped]` wiring.
- The `--uncommitted` git call **never** reaches network, config, or refs. Allowed argv is
  literally `["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"]`; any
  future git call in this plan requires an equally narrow whitelist in the same one place.
- Live sessions are excluded from `--all`, read from `<root>/sessions/*.json` per source
  (`PLAN.md` §3.2.2). A live-sid negative fixture is required — a transcript with a dangling
  `tool_use` **and** a live registry entry must not appear in the report.
- Subagent transcripts carry the **parent's** `sessionId` and identify themselves via
  `agentId` (`PLAN.md` §3.1); the corpus already handles this via `parse.py`. Nothing in this
  plan may collapse on `sessionId`.
- `tail_signal` derivation must never emit `completion_marker` on a session whose tail
  carries an unmatched `tool_use` (`mid_tool` wins in the precedence table).
- Skills invoke only `sk`; never `git` directly, never `~/.claude/**` reads (`PLAN.md`
  §5.2). The skill also carries the standard `$SK` resolver stanza from
  `skills/session-forensics/SKILL.md`.

---

## File Structure

**Created:**
- `sessionkit/live.py` — one function: `live_sids(sources: list[Source]) -> set[str]`.
  Reads `<root>/sessions/*.json` per source; returns the union of `sessionId` values. Silent
  on `FileNotFoundError`; logs nothing. Kept as its own file so the fixture harness can stub
  a `SESSIONKIT_LIVE_SIDS` env-var override without monkey-patching `sources.py`.
- `skills/unfinished-work/SKILL.md` — the new skill wrapping `sk tail --all` and
  `sk files --uncommitted` into "what is at risk of being lost."
- `tests/test_tail_signal.py` — pure classifier tests for `derive_tail_signal`, one positive
  and one negative fixture per value plus a precedence test.
- `tests/test_tail.py` — end-to-end tests for `sk tail <sid>` and `sk tail --all` through
  `cli.main`, including live-sid exclusion.
- `tests/test_files.py` — end-to-end tests for `sk files <sid>`, `sk files --project`, and
  `sk files <sid> --uncommitted` (the git portion uses a `tmpdir` git repo initialized in
  the test itself).

**Modified:**
- `sessionkit/classify.py` — adds `TAIL_SIGNALS` precedence tuple, `derive_tail_signal`
  function.
- `sessionkit/parse.py` — adds one field `tail_signal: str = ""` to `ParsedSession`.
- `sessionkit/corpus.py` — `load_one` calls `derive_tail_signal` (through the query wrapper)
  as another post-parse annotation, alongside `annotate_errors` and `derive_end_state`.
- `sessionkit/query.py` — adds `tail_context`, `tail_signal`, `tail_rows`, `tail_candidates`,
  `file_rows`, `file_project_rows`, `uncommitted_intersection` functions.
- `sessionkit/cli.py` — adds `cmd_tail` and `cmd_files`, their subparsers, and their entries
  in `COMMANDS`.
- `PLAN.md` — Phase 3 acceptance rewritten; `sk unfinished` → `sk tail` in §5.1; `gitlink.py`
  struck from Phase 3 references; skill list confirms `unfinished-work` shipped.

**Not modified:**
- `sessionkit/render.py` — `Report`, `_truncate_cell`, budgets already cover the new
  commands' needs.
- `sessionkit/redact.py` — new previews pass through the existing pipeline via
  `Report.table`.

---

## Task 1: Add `tail_signal` classifier

**Files:**
- Modify: `sessionkit/classify.py` (add ~60 lines around line 200, near `derive_end_state`)
- Modify: `sessionkit/parse.py:120-152` (add one dataclass field)
- Create: `tests/test_tail_signal.py`

**Interfaces:**
- Consumes: `ParsedSession` (existing dataclass), `ToolCall` (existing).
- Produces:
  - `classify.TAIL_SIGNALS: tuple[str, ...]` — precedence order, low index wins.
    Values (in order): `"mid_tool"`, `"error_tail"`, `"apology"`, `"completion_marker"`,
    `"next_step_stated"`, `"silent"`, `"neutral"`.
  - `classify.derive_tail_signal(session: ParsedSession, tail_texts: dict[int, str]) -> str`
    — pure function. `tail_texts` maps `message.line` to the message's full text (uncapped)
    for the last N messages the caller wants considered. Returns one string from
    `TAIL_SIGNALS`. Never raises.
  - `ParsedSession.tail_signal: str = ""` — new field, populated later by Task 3's wrapper.

- [ ] **Step 1: Add the `tail_signal` field to `ParsedSession`**

In `sessionkit/parse.py`, at line 152 (after `dispatch_edges`):

```python
    tail_signal: str = ""
```

- [ ] **Step 2: Write the failing tail-signal tests**

Create `tests/test_tail_signal.py`:

```python
"""Unit tests for classify.derive_tail_signal.

Every TAIL_SIGNALS value has one positive and one negative fixture. The final test pins the
precedence table so a later reordering fails loudly instead of quietly changing report ranks.
"""

from __future__ import annotations

import unittest

from sessionkit.classify import TAIL_SIGNALS, derive_tail_signal
from sessionkit.parse import Message, ParsedSession, ToolCall


def _session(*, messages: list[Message] | None = None,
             tools: list[ToolCall] | None = None) -> ParsedSession:
    return ParsedSession(sid="s", source_id="test", path="/x",
                         messages=list(messages or []), tools=list(tools or []))


def _msg(line: int, role: str, preview: str = "") -> Message:
    return Message(uuid=f"u{line}", line=line, ts="2026-01-01T00:00:00Z",
                   role=role, preview=preview, text_len=len(preview))


def _tool(line: int, name: str = "Bash", *, is_error: bool = False,
          err_class: str = "") -> ToolCall:
    return ToolCall(tool_use_id=f"t{line}", line=line, name=name, ts="2026-01-01T00:00:00Z",
                    is_error=is_error, err_class=err_class)


class TailSignalTest(unittest.TestCase):
    def test_precedence_order_is_pinned(self) -> None:
        self.assertEqual(TAIL_SIGNALS, (
            "mid_tool", "error_tail", "apology", "completion_marker",
            "next_step_stated", "silent", "neutral",
        ))

    def test_mid_tool_wins_over_completion_marker(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "The refactor is done and shipped.")],
            tools=[_tool(11, "Bash", is_error=False, err_class="no-result")],
        )
        self.assertEqual(derive_tail_signal(session, {10: "The refactor is done and shipped."}),
                         "mid_tool")

    def test_mid_tool_negative_when_all_tools_resolved(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "Neutral text.")],
            tools=[_tool(11, "Bash", is_error=False)],
        )
        self.assertEqual(derive_tail_signal(session, {10: "Neutral text."}), "neutral")

    def test_error_tail_when_final_tool_failed(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "Trying again.")],
            tools=[_tool(11, "Bash", is_error=True, err_class="exit-code")],
        )
        self.assertEqual(derive_tail_signal(session, {10: "Trying again."}), "error_tail")

    def test_error_tail_negative_when_no_result_is_the_tail(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "Trying again.")],
            tools=[_tool(11, "Bash", is_error=False, err_class="no-result")],
        )
        self.assertEqual(derive_tail_signal(session, {10: "Trying again."}), "mid_tool")

    def test_apology_matches_last_assistant_message(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "I'm sorry, I can't proceed without more context.")])
        self.assertEqual(
            derive_tail_signal(session,
                               {10: "I'm sorry, I can't proceed without more context."}),
            "apology")

    def test_apology_negative_when_text_is_neutral(self) -> None:
        session = _session(messages=[_msg(10, "assistant", "All caught up.")])
        self.assertEqual(derive_tail_signal(session, {10: "All caught up."}), "neutral")

    def test_completion_marker_matches_final_assistant_text(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "The migration is complete and tests pass.")])
        self.assertEqual(
            derive_tail_signal(session,
                               {10: "The migration is complete and tests pass."}),
            "completion_marker")

    def test_completion_marker_negative_when_word_is_absent(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "Working on the next step now.")])
        self.assertNotEqual(
            derive_tail_signal(session, {10: "Working on the next step now."}),
            "completion_marker")

    def test_next_step_stated_matches_forward_statement(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "I'll refactor the parser next.")])
        self.assertEqual(
            derive_tail_signal(session, {10: "I'll refactor the parser next."}),
            "next_step_stated")

    def test_next_step_stated_negative_when_message_is_past_tense(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "Refactored the parser.")])
        self.assertNotEqual(
            derive_tail_signal(session, {10: "Refactored the parser."}),
            "next_step_stated")

    def test_silent_when_final_turn_is_a_user_prompt(self) -> None:
        session = _session(messages=[_msg(10, "user", "any update?")])
        self.assertEqual(derive_tail_signal(session, {10: "any update?"}), "silent")

    def test_silent_negative_when_assistant_replied(self) -> None:
        session = _session(messages=[
            _msg(10, "user", "any update?"),
            _msg(11, "assistant", "yes, done."),
        ])
        self.assertEqual(derive_tail_signal(session,
                                            {10: "any update?", 11: "yes, done."}),
                         "completion_marker")

    def test_neutral_when_no_rule_matches(self) -> None:
        session = _session(messages=[_msg(10, "assistant", "Some ordinary text.")])
        self.assertEqual(derive_tail_signal(session, {10: "Some ordinary text."}), "neutral")

    def test_empty_session_is_neutral(self) -> None:
        self.assertEqual(derive_tail_signal(_session(), {}), "neutral")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_tail_signal -v`
Expected: `ImportError: cannot import name 'TAIL_SIGNALS' from 'sessionkit.classify'`.

- [ ] **Step 4: Implement `derive_tail_signal`**

In `sessionkit/classify.py`, after `derive_end_state` (around line 203), add:

```python
#: Precedence order — earlier entries win. Same shape as the error taxonomy's ordered rules:
#: a single signal per session, chosen deterministically, so a downstream skill can key on it.
TAIL_SIGNALS: tuple[str, ...] = (
    "mid_tool", "error_tail", "apology", "completion_marker",
    "next_step_stated", "silent", "neutral",
)

_APOLOGY_RE = re.compile(r"\b(sorry|apologi[sz]e|unable to|can'?t|cannot)\b", re.I)
_COMPLETION_RE = re.compile(
    r"\b(done|complete[ds]?|finished|shipped|ready|works|all set|"
    r"passing|passes)\b", re.I)
_NEXT_STEP_RE = re.compile(
    r"(?:^|[\.\n])\s*(?:I(?:'|’)?ll|I will|next[,:]|next step|going to|"
    r"about to|let me)\b", re.I)


def derive_tail_signal(session: ParsedSession, tail_texts: dict[int, str]) -> str:
    """Classify the tail of a session for the ``unfinished-work`` skill.

    Args:
        session: Post-parse session, with ``err_class`` already annotated on tools.
        tail_texts: Map from ``message.line`` to that message's **full** text — the
            classifier needs uncapped strings because completion markers ("this is now
            done") frequently land past the 200-char preview cap. The caller decides how
            many trailing messages to include; only the entries whose line matches a
            message in ``session.messages`` are consulted.

    Returns:
        One value from ``TAIL_SIGNALS``. Never raises. Empty sessions return ``"neutral"``.
    """
    # mid_tool: any tool_use without a paired tool_result. `annotate_errors` already tags
    # these with err_class == "no-result" (parse.py leaves the flag set to False because
    # the tool did not produce a failing result — it produced no result at all).
    if any(t.err_class == "no-result" for t in session.tools):
        return "mid_tool"

    # error_tail: the final tool call actually failed with a classified taxonomy error.
    if session.tools:
        last = session.tools[-1]
        if last.is_error and last.err_class and last.err_class != "no-result":
            return "error_tail"

    if not session.messages:
        return "neutral"

    last_msg = session.messages[-1]

    # silent: the final turn is a user prompt with no assistant reply — the same signal
    # `derive_end_state` uses for `interrupted-user`, exposed here for the tail classifier.
    if last_msg.role == "user":
        return "silent"

    if last_msg.role != "assistant":
        return "neutral"

    text = tail_texts.get(last_msg.line, last_msg.preview or "")
    if _APOLOGY_RE.search(text):
        return "apology"
    if _COMPLETION_RE.search(text):
        return "completion_marker"
    if _NEXT_STEP_RE.search(text):
        return "next_step_stated"
    return "neutral"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_tail_signal -v`
Expected: `Ran 14 tests in ...s / OK`.

- [ ] **Step 6: Run the full suite to catch collateral breakage**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: all tests pass (adds 14 new; every prior test still passes).

- [ ] **Step 7: Commit**

```bash
git add sessionkit/classify.py sessionkit/parse.py tests/test_tail_signal.py
git commit -m "classify: add derive_tail_signal for Phase 3 sk tail"
```

---

## Task 2: `sk tail <sid>` — session-scoped

**Files:**
- Modify: `sessionkit/parse.py` (add `read_line(path, line_no) -> dict | None` helper —
  a single-line JSON re-reader missing from the current tree despite an earlier draft's
  citation of a `parse.py:468` reference that did not survive to this branch)
- Modify: `sessionkit/query.py` (add ~60 lines)
- Modify: `sessionkit/corpus.py` (add one line in `load_one` calling the tail wrapper)
- Modify: `sessionkit/cli.py` (add `cmd_tail` handler + subparser)
- Create: `tests/test_tail.py` (session-scoped portion only)

**Interfaces:**
- Consumes: `classify.derive_tail_signal` (Task 1), `query.find_session` (existing at
  `query.py:220`), `render.Report` (existing).
- Introduces: `parse.read_line(path: str | Path, line_no: int) -> dict[str, Any] | None`
  — re-reads one 1-indexed line of a transcript, returns the decoded record or `None`
  if the file is unreadable, the line doesn't exist, or the payload isn't valid JSON /
  isn't a dict. `tail_context` uses this to see full message text past the 200-char
  preview cap; callers must tolerate `None` and fall back to the preview.
- Produces:
  - `query.tail_context(entry: Loaded, n: int = 6) -> dict[int, str]` — reads the last
    `n` messages of a session and returns a `{message.line: full_text}` map. Uses
    `parse.read_line` for uncapped text; falls back to `Message.preview` on read failure.
  - `query.tail_signal(entry: Loaded, n: int = 6) -> str` — thin wrapper that populates
    `entry.session.tail_signal` (memoised on the field), calling `derive_tail_signal`
    once per session.
  - `query.tail_rows(entry: Loaded, n: int = 6) -> list[Row]` — returns row dicts
    `{"line", "role", "chars", "preview"}` for the last N messages in ascending line
    order, mirroring `message_rows`.
  - `cli.cmd_tail(corpus, args)` — prints header (`sid`, `project`, `state`,
    `tail_signal`) + a table of the last N turns. Layer-3 excerpt budget
    (`BUDGET_EXCERPT_KB`, 8 KB).
  - `sk tail <sid> [--n N] [--full] [--json] [--budget-kb K]` on the CLI.

- [ ] **Step 1: Write the failing session-scoped tail tests**

Create `tests/test_tail.py`:

```python
"""End-to-end tests for `sk tail`.

The `--all` corpus tests are added in Task 3. Each test builds a real transcript via
`tests.fixtures` and drives `cli.main` so the whole stack (parse → classify → query → render)
is exercised.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sessionkit import cli
from tests import fixtures as fx


class TailSessionScoped(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "claude"
        self.home.mkdir()
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home),
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(list(argv)), 0)
        return buf.getvalue()

    def test_tail_prints_last_n_turns_with_signal(self) -> None:
        fx.write(self.home, [
            fx.user("start", ts="2026-08-01T00:00:00Z"),
            fx.assistant([{"type": "text", "text": "ack"}], ts="2026-08-01T00:00:01Z"),
            fx.user("more"),
            fx.assistant([{"type": "text", "text": "working"}],
                         ts="2026-08-01T00:00:03Z"),
            fx.user("more still"),
            fx.assistant([{"type": "text", "text": "The migration is complete."}],
                         ts="2026-08-01T00:00:05Z"),
        ], name=f"{fx.SID}.jsonl")
        out = self._run("tail", fx.SID, "--n", "4", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["meta"]["tail_signal"], "completion_marker")
        self.assertEqual(payload["meta"]["n"], 4)
        self.assertLessEqual(len(payload["tail"]), 4 + 4)  # <=N msg rows + trailing tools

    def test_tail_missing_session_exits_nonzero(self) -> None:
        fx.write(self.home, fx.simple_session(), name=f"{fx.SID}.jsonl")
        with self.assertRaises(SystemExit):
            self._run("tail", "deadbeef")

    def test_tail_completion_marker_reads_full_text_past_preview(self) -> None:
        # Message preview is capped at MSG_PREVIEW=200. Push the marker past that so the
        # signal is only found when tail_context re-reads the source line.
        long_text = ("x" * 300) + " the refactor is complete."
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([{"type": "text", "text": long_text}],
                         ts="2026-08-01T00:00:01Z"),
        ], name=f"{fx.SID}.jsonl")
        out = self._run("tail", fx.SID, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["meta"]["tail_signal"], "completion_marker")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_tail -v`
Expected: `AttributeError: module 'sessionkit.cli' has no attribute 'cmd_tail'` (or the
argparse error "invalid choice: 'tail'"), across all three tests.

- [ ] **Step 3: Add the `query.py` functions**

In `sessionkit/query.py`, add:

```python
def tail_context(entry: Loaded, n: int = 6) -> dict[int, str]:
    """Read the last ``n`` messages of a session and return full text keyed by line.

    Uses ``parse.read_line`` so ``tail_signal`` classification sees the full message text
    rather than the 200-char preview — completion markers routinely land past the cap.
    A message that cannot be re-read falls back to its preview so the classifier never
    starves.
    """
    from sessionkit import parse  # local import: parse.read_line is already imported at
                                  # module scope in most callers, but keep classify's
                                  # dependency direction clean.
    result: dict[int, str] = {}
    tail = entry.session.messages[-n:] if n > 0 else entry.session.messages
    for message in tail:
        record = parse.read_line(entry.session.path, message.line)
        text = ""
        if record is not None:
            content = ((record.get("message") or {}).get("content")
                       if isinstance(record.get("message"), dict) else None)
            text = parse.block_text(content) if content is not None else ""
        result[message.line] = text or (message.preview or "")
    return result


def tail_signal(entry: Loaded, n: int = 6) -> str:
    """Compute (and memoise on the ParsedSession) the tail signal."""
    from sessionkit.classify import derive_tail_signal
    if entry.session.tail_signal:
        return entry.session.tail_signal
    signal = derive_tail_signal(entry.session, tail_context(entry, n))
    entry.session.tail_signal = signal
    return signal


def tail_rows(entry: Loaded, n: int = 6, full: bool = False) -> list[Row]:
    """Row shape for `sk tail <sid>`: last N messages plus their trailing tool calls."""
    tail = entry.session.messages[-n:] if n > 0 else entry.session.messages
    if not tail:
        return []
    lo = tail[0].line
    rows = message_rows(entry, lo, 10_000, full=full)
    # Interleave any tool calls whose line falls inside the tail window so the reader
    # sees mid_tool context in place, not out of band.
    tool_rows_in_window = [
        {"line": t.line, "role": "tool", "chars": len(t.input_preview or ""),
         "preview": f"{t.name}: {t.input_preview or ''}"}
        for t in entry.session.tools if t.line >= lo
    ]
    combined = sorted(rows + tool_rows_in_window, key=lambda r: r["line"])
    return combined
```

- [ ] **Step 4: Wire `tail_signal` into the post-parse pipeline**

In `sessionkit/corpus.py`, in `load_one` (around line 86), after `derive_end_state` runs
(check the current call site — it happens in `parse.parse_file` or the corpus wrapper),
call `query.tail_signal(loaded, n=6)` once per session so the field is populated for
`--all` scans without every caller re-reading the tail. If the annotation currently lives
in `parse.parse_file`, add the wrapper there instead; the goal is one call per session at
load time.

- [ ] **Step 5: Add the `cmd_tail` handler and subparser in `cli.py`**

In `sessionkit/cli.py`, after `cmd_children` (around line 288), add:

```python
def cmd_tail(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 3: the last N turns of one session, plus its tail signal.

    Judgment about `done vs unfinished` lives in the `unfinished-work` skill; this command
    surfaces the material and one deterministic classification, nothing more.
    """
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    signal = query.tail_signal(entry, n=args.n)

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB, full=args.full)
    report.meta(sid=entry.session.sid, project=entry.project_key,
                state=entry.session.end_state, tail_signal=signal,
                n=args.n, turns=entry.session.turns)
    report.section(f"Tail (last {args.n})")
    report.table(["line", "role", "chars", "preview"],
                 [[r["line"], r["role"], r["chars"], r["preview"]]
                  for r in query.tail_rows(entry, n=args.n, full=args.full)],
                 key="tail")
    return report.render()
```

And in `build_parser`, after the `children` subparser (around line 489), add:

```python
    p_tail = sub.add_parser("tail", parents=[common],
                            help="last N turns of one session, with a tail signal")
    p_tail.add_argument("sid", help="session id or unique prefix")
    p_tail.add_argument("--n", type=int, default=6,
                        help="number of trailing turns to include (default: 6)")
    p_tail.add_argument("--full", action="store_true",
                        help="re-read message text from source for full fidelity "
                             "(JSON never truncates a cell either way)")
```

Finally, add `"tail": cmd_tail` to the `COMMANDS` dict at line 493.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_tail -v`
Expected: three tests pass.

Then run the full suite: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: all prior tests still pass. If a prior test asserted an exact CLI help
string, update it to include the new `tail` subcommand.

- [ ] **Step 7: Commit**

```bash
git add sessionkit/query.py sessionkit/corpus.py sessionkit/cli.py tests/test_tail.py
git commit -m "sk tail: session-scoped last-N-turns command with tail signal"
```

---

## Task 3: `sk tail --all` — corpus scan, live-session exclusion

**Files:**
- Create: `sessionkit/live.py`
- Modify: `sessionkit/query.py` (add `tail_candidates`, ~40 lines)
- Modify: `sessionkit/cli.py` (extend `cmd_tail`, add `--all` and `--n` flags to the
  existing subparser)
- Modify: `tests/test_tail.py` (add `TailCorpusScoped` class)

**Interfaces:**
- Consumes: `sessionkit.sources.discover` (existing at `sources.py`), `Loaded` iterator
  from the corpus, `query.tail_signal` (Task 2).
- Produces:
  - `live.live_sids(source_roots: Iterable[Path]) -> set[str]` — reads
    `<root>/sessions/*.json` per root; returns the union of `sessionId` fields.
    `FileNotFoundError` yields the empty set silently; malformed JSON is skipped
    with a debug-only counter (not logged in tests). Supports an
    `SESSIONKIT_LIVE_SIDS` env-var override (comma-separated sids) so a test never
    depends on real live state.
  - `query.tail_candidates(corpus, scope, *, exclude_live: bool = True) -> list[Loaded]`
    — returns sessions where `end_state != "complete"`; when `exclude_live=True`,
    additionally filters out any session whose sid is in `live.live_sids`.

- [ ] **Step 1: Write the failing corpus-scoped tests**

Extend `tests/test_tail.py` with a `TailCorpusScoped` class. Reuse the setUp/_run helpers
from `TailSessionScoped` (either by inheritance or by pulling them into a shared mixin):

```python
class TailCorpusScoped(TailSessionScoped):
    def _mid_tool_transcript(self, sid: str) -> None:
        # Assistant turn issues a Bash tool_use with no matching tool_result.
        fx.write(self.home, [
            fx.user("run something"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "sleep 60"})]),
        ], name=f"{sid}.jsonl")

    def test_all_lists_non_complete_sessions_only(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")  # complete
        self._mid_tool_transcript("bbbb2222")  # interrupted-tool
        out = self._run("tail", "--all", "--json")
        payload = json.loads(out)
        sids = [row[0] for row in payload["candidates"]]
        self.assertIn("bbbb2222"[:8], sids)
        self.assertNotIn("aaaa1111"[:8], sids)

    def test_all_excludes_live_sessions(self) -> None:
        self._mid_tool_transcript("bbbb2222")
        with mock.patch.dict(os.environ, {"SESSIONKIT_LIVE_SIDS": "bbbb2222"}):
            out = self._run("tail", "--all", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["candidates"], [],
                         "a live session must not appear as unfinished work")

    def test_all_row_has_tail_signal(self) -> None:
        self._mid_tool_transcript("bbbb2222")
        out = self._run("tail", "--all", "--json")
        payload = json.loads(out)
        row = payload["candidates"][0]
        # columns: sid, state, tail_signal, ended, tail_excerpt
        self.assertEqual(row[2], "mid_tool")

    def test_all_emits_omitted_count_when_budget_forces_drops(self) -> None:
        for sid_suffix in "abcdef":
            self._mid_tool_transcript(sid_suffix * 8)
        out = self._run("tail", "--all", "--budget-kb", "0.4")
        self.assertIn("more row(s) omitted", out)
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_tail.TailCorpusScoped -v`
Expected: failures/errors — `--all` flag not known, `live` module missing, etc.

- [ ] **Step 3: Create `sessionkit/live.py`**

```python
"""Live-session discovery.

Claude Code writes ``<claude_root>/sessions/<pid>.json`` for every running session and
removes the file on exit (``PLAN.md`` §3.2.2). The registry is what makes ``sk tail --all``
honest about which of its `interrupted-tool` candidates are actually running right now
versus abandoned — without this filter, every concurrent session shows up as unfinished
work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


def live_sids(source_roots: Iterable[Path]) -> set[str]:
    """Return every sessionId currently listed in a ``sessions/`` registry.

    ``SESSIONKIT_LIVE_SIDS`` (comma-separated) overrides the on-disk read entirely,
    which is what tests use — no real registry needs to exist under the fixture root.
    """
    override = os.environ.get("SESSIONKIT_LIVE_SIDS")
    if override is not None:
        return {sid.strip() for sid in override.split(",") if sid.strip()}
    sids: set[str] = set()
    for root in source_roots:
        registry = Path(root) / "sessions"
        try:
            entries = list(registry.iterdir())
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        for entry in entries:
            if entry.suffix != ".json":
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            sid = data.get("sessionId") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                sids.add(sid)
    return sids
```

- [ ] **Step 4: Add `tail_candidates` in `query.py`**

```python
def tail_candidates(corpus: Corpus, scope: Filter,
                    *, exclude_live: bool = True) -> list[Loaded]:
    """Sessions eligible for `sk tail --all`: not `complete`, and not currently running."""
    from sessionkit import live, sources as src_mod
    live_set: set[str] = set()
    if exclude_live:
        roots = {s.root for s in src_mod.discover() if s.reachable}
        live_set = live.live_sids(roots)
    return [
        entry for entry in scope.apply(corpus)
        if entry.session.end_state != "complete" and entry.session.sid not in live_set
    ]
```

`Filter.apply(corpus) -> list[Loaded]` (query.py:71) already applies the
project/source/since/state/subagents/label_contains predicates — do not duplicate that
logic here.

- [ ] **Step 5: Extend `cmd_tail` for `--all`**

In `cli.py`, extend the subparser to add `--all` and the shared scope flags:

```python
    p_tail = sub.add_parser("tail", parents=[common, scoped],
                            help="last N turns of one session, with a tail signal")
    group = p_tail.add_mutually_exclusive_group(required=True)
    group.add_argument("sid", nargs="?", default=None,
                       help="session id or unique prefix")
    group.add_argument("--all", action="store_true",
                       help="scan every non-complete session in scope (excludes live "
                            "sessions from `sessions/*.json`)")
    p_tail.add_argument("--n", type=int, default=6,
                        help="number of trailing turns to include (default: 6)")
    p_tail.add_argument("--full", action="store_true",
                        help="re-read message text from source for full fidelity")
    _subagents_arg(p_tail, "include")
```

Extend `cmd_tail`:

```python
def cmd_tail(corpus: Corpus, args: argparse.Namespace) -> str:
    if getattr(args, "all", False):
        return _cmd_tail_all(corpus, args)
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    # ...existing single-session body from Task 2.


def _cmd_tail_all(corpus: Corpus, args: argparse.Namespace) -> str:
    candidates = query.tail_candidates(corpus, _scope(args))
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB, full=args.full)
    report.meta(candidates=len(candidates), n=args.n)
    report.section("Candidates (non-complete, not currently running)")
    rows: list[list] = []
    for entry in candidates:
        signal = query.tail_signal(entry, n=args.n)
        last = entry.session.messages[-1] if entry.session.messages else None
        excerpt = ""
        if last is not None:
            tail_texts = query.tail_context(entry, n=1)
            excerpt = tail_texts.get(last.line, last.preview or "")
        rows.append([entry.session.sid[:8], entry.session.end_state, signal,
                     (entry.session.ended_at or "")[:16], excerpt])
    report.table(["sid", "state", "tail_signal", "ended", "tail_excerpt"], rows,
                 key="candidates")
    return report.render()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_tail -v`
Expected: all tests in this file pass.

Full suite: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`

- [ ] **Step 7: Commit**

```bash
git add sessionkit/live.py sessionkit/query.py sessionkit/cli.py tests/test_tail.py
git commit -m "sk tail --all: corpus scan with live-session exclusion"
```

---

## Task 4: `sk files` — session and project scoping (no git)

**Files:**
- Modify: `sessionkit/query.py` (add `file_rows`, `file_project_rows`, ~50 lines)
- Modify: `sessionkit/cli.py` (add `cmd_files` + subparser)
- Create: `tests/test_files.py` (session + project cases; the git portion belongs to
  Task 5)

**Interfaces:**
- Consumes: `ParsedSession.files` (list of `FileOp`, existing).
- Produces:
  - `query.file_rows(entry: Loaded) -> list[Row]` — per unique path in one session:
    `{"path", "reads", "writes", "edits", "first_line", "last_line", "first_ts",
     "last_ts"}`. Sorted by descending total ops, ties broken by first_line ascending.
  - `query.file_project_rows(corpus, scope) -> list[Row]` — per unique path across a
    project (or corpus): the same columns plus `"sessions"` (session count) and
    `"exemplar_sid"` (any session that touched it).
  - `cmd_files` handler + `sk files [<sid>] [--project NAME] [--json] [--budget-kb K]`
    on the CLI.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_files.py`:

```python
"""End-to-end tests for `sk files`.

Session-scoped and project-scoped shapes; the `--uncommitted` git intersection is Task 5.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sessionkit import cli
from tests import fixtures as fx


class FilesSessionScoped(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "claude"
        self.home.mkdir()
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home),
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(list(argv)), 0)
        return buf.getvalue()

    def _mixed_ops_transcript(self) -> None:
        fx.write(self.home, [
            fx.user("edit stuff"),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/home/dev/myproject/a.py"})]),
            fx.tool_result("t1", "content"),
            fx.assistant([fx.tool_use("t2", "Edit",
                                       {"file_path": "/home/dev/myproject/a.py",
                                        "old_string": "x", "new_string": "y"})],
                         ts="2026-08-01T00:00:03Z"),
            fx.tool_result("t2", "edited", ts="2026-08-01T00:00:04Z"),
            fx.assistant([fx.tool_use("t3", "Write",
                                       {"file_path": "/home/dev/myproject/b.py",
                                        "content": "print('hi')"})],
                         ts="2026-08-01T00:00:05Z"),
            fx.tool_result("t3", "wrote", ts="2026-08-01T00:00:06Z"),
        ], name=f"{fx.SID}.jsonl")

    def test_session_scoped_splits_op_counts(self) -> None:
        self._mixed_ops_transcript()
        out = self._run("files", fx.SID, "--json")
        payload = json.loads(out)
        rows = {r[0]: r for r in payload["files"]}
        self.assertEqual(rows["/home/dev/myproject/a.py"][1:4], [1, 0, 1])  # R, W, E
        self.assertEqual(rows["/home/dev/myproject/b.py"][1:4], [0, 1, 0])

    def test_missing_sid_exits_nonzero(self) -> None:
        self._mixed_ops_transcript()
        with self.assertRaises(SystemExit):
            self._run("files", "deadbeef")

    def test_project_rollup_aggregates_sessions(self) -> None:
        self._mixed_ops_transcript()
        # A second session in the same cwd touches a.py again.
        fx.write(self.home, [
            fx.user("more"),
            fx.assistant([fx.tool_use("t9", "Read",
                                       {"file_path": "/home/dev/myproject/a.py"})]),
            fx.tool_result("t9", "content"),
        ], name="ccccdddd.jsonl")
        out = self._run("files", "--json")  # corpus/project scope
        payload = json.loads(out)
        a_row = next(r for r in payload["files"] if r[0] == "/home/dev/myproject/a.py")
        self.assertEqual(a_row[1], 2, "two sessions touched a.py")

    def test_unknown_project_returns_empty_table_not_error(self) -> None:
        self._mixed_ops_transcript()
        out = self._run("files", "--project", "nonexistent", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["files"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
```

- [ ] **Step 2: Run to verify tests fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_files -v`
Expected: errors — `cmd_files` not defined, `sk files` subcommand missing.

- [ ] **Step 3: Implement `file_rows` and `file_project_rows` in `query.py`**

```python
def file_rows(entry: Loaded) -> list[Row]:
    """One row per unique path in a session, with op counts split by kind."""
    buckets: dict[str, dict[str, Any]] = {}
    for op in entry.session.files:
        row = buckets.setdefault(op.path, {
            "path": op.path, "reads": 0, "writes": 0, "edits": 0,
            "first_line": op.line, "last_line": op.line,
            "first_ts": op.ts, "last_ts": op.ts,
        })
        if op.op == "read":
            row["reads"] += 1
        elif op.op == "write":
            row["writes"] += 1
        elif op.op == "edit":
            row["edits"] += 1
        row["last_line"] = max(row["last_line"], op.line)
        row["last_ts"] = op.ts if op.ts > row["last_ts"] else row["last_ts"]
    return sorted(
        buckets.values(),
        key=lambda r: (-(r["reads"] + r["writes"] + r["edits"]), r["first_line"]),
    )


def file_project_rows(corpus: Corpus, scope: Filter) -> list[Row]:
    """Per-path rollup across every session in scope."""
    buckets: dict[str, dict[str, Any]] = {}
    for entry in scope.apply(corpus):
        for op in entry.session.files:
            row = buckets.setdefault(op.path, {
                "path": op.path, "reads": 0, "writes": 0, "edits": 0,
                "sessions": set(), "exemplar_sid": entry.session.sid,
            })
            if op.op == "read":
                row["reads"] += 1
            elif op.op == "write":
                row["writes"] += 1
            elif op.op == "edit":
                row["edits"] += 1
            row["sessions"].add(entry.session.sid)
    # Materialise session set → count and drop the exemplar to a short prefix for display.
    out = []
    for row in buckets.values():
        row["sessions"] = len(row["sessions"])
        out.append(row)
    return sorted(out, key=lambda r: (-r["sessions"],
                                      -(r["reads"] + r["writes"] + r["edits"]),
                                      r["path"]))
```

- [ ] **Step 4: Add `cmd_files` and the subparser in `cli.py`**

```python
def cmd_files(corpus: Corpus, args: argparse.Namespace) -> str:
    """Files touched by a session (or across a project)."""
    if args.sid:
        entry = query.find_session(corpus, args.sid)
        if entry is None:
            raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
        rows = query.file_rows(entry)
        report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
        report.meta(sid=entry.session.sid, project=entry.project_key,
                    cwd=entry.session.cwd, paths=len(rows))
        report.section("Files")
        report.table(
            ["path", "reads", "writes", "edits", "first_line", "last_line"],
            [[r["path"], r["reads"], r["writes"], r["edits"],
              r["first_line"], r["last_line"]] for r in rows],
            key="files",
        )
        return report.render()

    rows = query.file_project_rows(corpus, _scope(args))
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(paths=len(rows), project=args.project or "any")
    report.section("Files across scope")
    report.table(
        ["path", "sessions", "reads", "writes", "edits", "exemplar_sid"],
        [[r["path"], r["sessions"], r["reads"], r["writes"], r["edits"],
          r["exemplar_sid"][:8]] for r in rows],
        key="files",
    )
    return report.render()
```

And in `build_parser`:

```python
    p_files = sub.add_parser("files", parents=[common, scoped],
                             help="files a session (or scope) touched")
    p_files.add_argument("sid", nargs="?", default=None,
                         help="session id or unique prefix; omit for a project rollup")
    _subagents_arg(p_files, "include")
```

Register in `COMMANDS`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_files -v`
Expected: all tests pass.

Full suite: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_files.py
git commit -m "sk files: session-scoped and project-scoped file aggregation"
```

---

## Task 5: `sk files --uncommitted` — git-status intersection

**Files:**
- Modify: `sessionkit/query.py` (add `uncommitted_intersection`, ~40 lines)
- Modify: `sessionkit/cli.py` (add `--uncommitted` flag; branch in `cmd_files`)
- Modify: `tests/test_files.py` (add `UncommittedIntersection` class)

**Interfaces:**
- Produces:
  - `query.uncommitted_intersection(cwd: str, paths: list[str]) -> tuple[set[str], str]`
    — returns `(dirty_paths, note)` where `dirty_paths` is the subset of `paths` that
    currently show up in `git status --porcelain=v1 -z` for `cwd`. `note` is empty on
    success, or a short human string when the intersection could not be performed
    (`"not a git repo"`, `"git binary not found"`, `"git status timed out"`). Never
    raises.
  - `sk files <sid> --uncommitted` on the CLI, plus `sk files --uncommitted` for
    project scope (intersects against the exemplar session's cwd — noting that only one
    cwd is checked, per row `note` column, when the aggregation covers multiple).

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_files.py` with a `UncommittedIntersection` class using a real git repo
built in a temp dir. The fixture SID's `cwd` is `/home/dev/myproject` (see
`tests/fixtures.py`), so build fixture transcripts whose file paths are `cwd`-relative
to the temp git repo instead. Since the fixture pins `CWD`, override it per test:

```python
class UncommittedIntersection(FilesSessionScoped):
    def _init_git_repo(self, root: Path) -> None:
        import subprocess
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
        (root / "README.md").write_text("hello\n")
        (root / "other.md").write_text("hi\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True,
                       env=env)

    def _session_touching(self, root: Path, files: list[str]) -> None:
        # Rewrite CWD in fixture calls by mutating the module attribute for this test.
        orig_cwd = fx.CWD
        fx.CWD = str(root)
        try:
            records = [fx.user("edit")]
            for i, path in enumerate(files, start=1):
                records.append(
                    fx.assistant([fx.tool_use(f"t{i}", "Edit",
                                              {"file_path": path, "old_string": "x",
                                               "new_string": "y"})],
                                 ts=f"2026-08-01T00:00:0{i}Z"))
                records.append(fx.tool_result(f"t{i}", "ok",
                                              ts=f"2026-08-01T00:00:1{i}Z"))
            fx.write(self.home, records, name=f"{fx.SID}.jsonl")
        finally:
            fx.CWD = orig_cwd

    def test_dirty_file_appears_when_session_touched_it(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        self._init_git_repo(repo)
        (repo / "README.md").write_text("modified\n")  # dirty
        self._session_touching(repo, [str(repo / "README.md"), str(repo / "other.md")])
        out = self._run("files", fx.SID, "--uncommitted", "--json")
        payload = json.loads(out)
        paths = {r[0] for r in payload["files"]}
        self.assertIn(str(repo / "README.md"), paths)
        self.assertNotIn(str(repo / "other.md"), paths)

    def test_ignored_file_never_appears(self) -> None:
        repo = self.tmp / "repo2"
        repo.mkdir()
        self._init_git_repo(repo)
        (repo / ".gitignore").write_text("secrets.env\n")
        (repo / "secrets.env").write_text("token=abc\n")
        self._session_touching(repo, [str(repo / "secrets.env")])
        out = self._run("files", fx.SID, "--uncommitted", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["files"], [],
                         "gitignored files are not listed by git status; the intersection "
                         "correctly drops them")

    def test_not_a_repo_notes_the_reason(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self._session_touching(plain, [str(plain / "a.py")])
        out = self._run("files", fx.SID, "--uncommitted")
        self.assertIn("not a git repo", out)

    def test_missing_git_binary_notes_the_reason(self) -> None:
        repo = self.tmp / "repo3"
        repo.mkdir()
        self._init_git_repo(repo)
        self._session_touching(repo, [str(repo / "README.md")])
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            out = self._run("files", fx.SID, "--uncommitted")
        self.assertIn("git binary not found", out)
```

- [ ] **Step 2: Run to verify tests fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_files.UncommittedIntersection -v`
Expected: attribute errors — flag not present.

- [ ] **Step 3: Implement `uncommitted_intersection` in `query.py`**

```python
import subprocess

_GIT_STATUS_ARGV = (
    "git", "status", "--porcelain=v1", "-z", "--untracked-files=normal",
)


def uncommitted_intersection(cwd: str, paths: list[str]) -> tuple[set[str], str]:
    """Return the subset of ``paths`` that are dirty in ``cwd``'s git tree.

    Uses a fixed argv so the call cannot mutate the tree, index, config or refs. Any
    non-zero exit is treated as "not a repo" or a similarly benign local condition —
    callers get an empty set and a short note rather than an exception.
    """
    try:
        completed = subprocess.run(
            _GIT_STATUS_ARGV, cwd=cwd, check=False, timeout=15,
            capture_output=True, env={"PATH": os.environ.get("PATH", ""),
                                      "HOME": os.environ.get("HOME", "")},
        )
    except FileNotFoundError:
        return (set(), "git binary not found")
    except subprocess.TimeoutExpired:
        return (set(), "git status timed out")
    if completed.returncode != 0:
        return (set(), "not a git repo" if b"not a git repository"
                in completed.stderr.lower() else
                f"git status failed (exit {completed.returncode})")
    dirty_rel: set[str] = set()
    # Porcelain -z separates entries with NUL; each entry is `XY <space> path`.
    # Renames are `XY <space> new-path NUL old-path NUL`; strip both.
    payload = completed.stdout.decode("utf-8", errors="replace")
    for entry in payload.split("\x00"):
        if len(entry) < 4:
            continue
        rel = entry[3:]
        dirty_rel.add(rel)
    # Intersect against the requested paths by both absolute and cwd-relative forms.
    session_rels = {p: _relative_to(cwd, p) for p in paths}
    return ({p for p, rel in session_rels.items() if rel in dirty_rel}, "")


def _relative_to(cwd: str, path: str) -> str:
    """Best-effort cwd-relative path, tolerant of separators and absolute forms."""
    from pathlib import PurePath
    try:
        return str(PurePath(path).relative_to(PurePath(cwd)))
    except ValueError:
        return path
```

- [ ] **Step 4: Wire `--uncommitted` into `cmd_files`**

Extend the subparser:

```python
    p_files.add_argument("--uncommitted", action="store_true",
                         help="intersect with `git status --porcelain` in the session's cwd")
```

Branch in `cmd_files`: when `args.uncommitted` and a sid is given, drop non-dirty rows
and add a `note` line to the header if the git call did not succeed:

```python
    if args.uncommitted:
        dirty, note = query.uncommitted_intersection(
            entry.session.cwd, [r["path"] for r in rows])
        rows = [r for r in rows if r["path"] in dirty]
        report.meta(uncommitted=len(rows))
        if note:
            report.text(f"git join: {note}")
```

For project scope with `--uncommitted`, iterate per exemplar cwd — one intersection call
per distinct cwd — and drop rows whose file was clean everywhere it was touched.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_files -v`
Expected: all tests pass.

Full suite: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_files.py
git commit -m "sk files --uncommitted: git status intersection"
```

---

## Task 6: skill `unfinished-work` + PLAN.md updates

**Files:**
- Create: `skills/unfinished-work/SKILL.md`
- Modify: `PLAN.md` — Phase 3 acceptance rewritten; §5.1 planned list; §5.2 skill list;
  strike `gitlink.py`.

**Interfaces:**
- Consumes: `sk tail --all --json`, `sk tail <sid> --json`, `sk files <sid>
  --uncommitted --json`, `sk index` — the same shape the other skills use.
- Produces: a markdown skill file that Claude Code routes on the front-matter
  `description` line.

- [ ] **Step 1: Draft the SKILL.md**

Create `skills/unfinished-work/SKILL.md`, mirroring the shape of
`skills/session-forensics/SKILL.md`:

- Front-matter with `name`, `description` — the description names the trigger phrases
  ("what did I have in flight", "resume", "what was I working on", "unfinished work",
  "abandoned sessions", "what's at risk of being lost") and lists the boundary skills.
- Boundaries table pointing "which session broke" queries at `session-forensics`,
  fleet-wide error questions at `error-patterns`, cost questions at `cost-forensics`.
- The standard `$SK` resolver stanza copied verbatim from
  `skills/session-forensics/SKILL.md`.
- Procedure:
  1. `$SK tail --all --json --n 4 --budget-kb 12` — get candidates.
  2. For each candidate, `$SK files <sid> --uncommitted --json` — annotate with dirty
     files.
  3. Bucket by `tail_signal` + dirty count + age:
     - `mid_tool` + dirty + recent (<24h): **Resume now**.
     - `apology` / `silent` / `error_tail` + dirty: **Review before resuming**.
     - `completion_marker` and clean: **Probably done** (report at bottom).
     - `killed-agents` / `interrupted-user` and clean: **Sunk** (close).
  4. For each Resume/Review, print a handoff brief: session title (from index),
     first prompt, last completed step (from `sk tail <sid>`), in-flight tool call
     (if `mid_tool`), uncommitted files (from `sk files`), stated next step (if
     `next_step_stated` fired), and a paste-ready `claude --resume <sid>` line.
  5. If the session was a subagent, note the parent sid — resuming means resuming the
     parent.
  6. Report state: candidates found, buckets, and one line per bucket count.

- [ ] **Step 2: Update `PLAN.md`**

Apply these edits:

- `PLAN.md` §5.1 — replace `unfinished` with `tail` in the Planned list. Update the
  count from 10 to 10 (unchanged; it's a rename).
- `PLAN.md` §7 Phase 3 header — rename to `sk tail`, `sk files`, skill
  `unfinished-work`. Strike `gitlink.py`. Rewrite the acceptance list to match this
  plan's task acceptances (tail_signal precedence, live-sid exclusion negative
  fixture, `--uncommitted` intersection notes, skill emits buckets).
- Any mention of `sk unfinished` elsewhere in `PLAN.md` (§5.3, §10) — rename to
  `sk tail`.
- No changes to `SPEC.md` or `NOTES.md` are in scope for this plan.

- [ ] **Step 3: Manually smoke-test the skill's invocation shape**

From `/workspace`, run:

```bash
./sk tail --all --json --n 4 --budget-kb 12 | python3 -m json.tool | head -60
```

Confirm the JSON has `candidates` with `sid`, `state`, `tail_signal`, `ended`,
`tail_excerpt` fields. Then pick one candidate sid and run:

```bash
./sk tail <sid> --n 6
./sk files <sid> --uncommitted
```

Confirm both work. This is not an automated test — it's a sanity check that the skill's
documented shell commands actually produce what the skill's procedure says they will.

- [ ] **Step 4: Run the full suite one last time**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/unfinished-work/SKILL.md PLAN.md
git commit -m "skill unfinished-work + PLAN.md Phase 3 rename"
```
