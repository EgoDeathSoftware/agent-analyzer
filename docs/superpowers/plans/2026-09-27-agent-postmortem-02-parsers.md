# Checkpoint 2: Data model and parsers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this checkpoint task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Part of:** the agent post-mortem plan. Tracker: `2026-09-27-agent-postmortem-00-tracker.md`. Read its **Global Constraints**, **Spec deviations** and **Review Focus** before starting; they apply to every task here.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**Goal:** Parse-time features and new `Message`/`ParsedSession` fields in both parsers; the pi and Claude Code parser bugs are fixed; pi-subagents async runs are loaded and linked.

**Starts from:** Checkpoint 1 is complete: its tasks are ticked in the tracker and the full suite passes.

**Done when:** every task below is committed; `PYTHONPATH=. python3 -m unittest discover -s tests -t .` passes; and the tracker marks each task done with its commit.

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
