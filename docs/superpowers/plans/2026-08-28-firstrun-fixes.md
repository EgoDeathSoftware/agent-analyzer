# FIRSTRUN fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the concrete gaps `FIRSTRUN.md` found during the first real dogfooding run —
lost anomaly line numbers, no parent→child subagent navigation, a preview cap that can hand back
invalid embedded JSON, and three smaller ergonomics gaps — without re-opening any decision
`PLAN.md`/`SPEC.md` already made.

**Architecture:** Two of the seven tasks are a **shared foundation** two different clients need
(§2 of `design-cross-cutting-work`): the exact `tool_use_id → child sid` join FIRSTRUN.md §8
proved is required (a heuristic/positional pairing silently mispaired a session's cost table),
which both `sk children` (Task 4, built here) and `sk cost --subagents` (Phase 5, not yet built)
depend on; and the source-line re-read primitive Task 5 introduces for `sk show --full`, which is
the general mechanism `SPEC.md` §4 already prescribes ("truncate at the renderer … offer `--full`
for one row"). Everything else is a localized bug fix or a single-command flag — those stay in
their own task rather than being generalized ahead of a second caller, per the same skill's
"prefer simplicity" rule.

**Tech Stack:** Python 3.12 stdlib only (matches `PLAN.md` §3); `unittest`, inline fixtures.

**Spec:** `FIRSTRUN.md` (this plan implements its §2–§6 findings in full, and lands the design
for §8's two smaller asks as a `PLAN.md` amendment rather than code, since `sk cost` itself is
still Phase 5 and out of scope here). Background: `PLAN.md` (what's shipped, phase boundaries),
`SPEC.md` §4 (the cell-truncation contract Task 5 completes).

## Global Constraints

- Runtime and tests are **stdlib-only** — no new dependencies (`PLAN.md` §3).
- Tests run via `PYTHONPATH=. python3 -m unittest discover -s tests -t .` (`PLAN.md` §8). Every
  new detector/parser field needs a positive **and** a negative fixture case.
- `sk` is **read-only** — no task here writes anything outside `stdout` (`PLAN.md` §9).
- **No silent truncation of rows; `--json` never truncates a cell** (`SPEC.md` §4). Any new
  full-text path must still pass through `redact.py` — skipping the length cap is not license to
  skip redaction (`PLAN.md` §9).
- Global flags (`--json`, `--budget-kb`) must keep parsing before **or** after the subcommand
  (`cli.py`'s `common` parser pattern) — new subcommands/flags follow the existing
  `parents=[common, scoped]` wiring, not a one-off.
- A fact lives in exactly one of `SPEC.md` / `PLAN.md` / `NOTES.md` (`SPEC.md` §6). Task 9 edits
  `PLAN.md` only, and only where it already owns the relevant fact.

---

## Task 1: `file-thrash`/`read-loop` findings carry real line numbers

**Files:**
- Modify: `sessionkit/parse.py:87-95` (`FileOp`), `sessionkit/parse.py:303-312` (`_file_op`)
- Modify: `sessionkit/classify.py:229-236` (`_path_churn`)
- Modify: `tests/test_classify.py:158,163,168,233` (existing `FileOp(...)` fixture calls)
- Test: `tests/test_parse.py`, `tests/test_classify.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `FileOp.line: int` (every later task that reads `session.files` can now cite a line);
  `_path_churn` anomalies now carry real `lines`, matching every other detector's contract.

Root cause (`FIRSTRUN.md` §3): `_path_churn` hardcodes `Anomaly(kind, path, n, [])` — the line
list is never populated — because `FileOp` has no `line` field to populate it from, even though
the `ToolCall` it's built from carries one (`parse.py:293`). `_error_cascade` does this correctly
today (`lines.append(call.line)`); `_path_churn` is the one detector that doesn't.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parse.py — inside class ParseTest, near test_records_file_operations
def test_file_operations_carry_their_line_number(self) -> None:
    session = self.parse([
        fx.user("go"),
        fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/a.py"})]),
    ])
    self.assertEqual(session.files[0].line, 2)  # the assistant record is JSONL line 2
```

```python
# tests/test_classify.py — inside class DetectorTest, replacing the four FileOp(...) fixture
# calls below with line-carrying ones, and adding an explicit lines assertion
def test_file_thrash(self) -> None:
    session = _session()
    session.files.extend(FileOp("/a.py", "edit", "", "t", line=i) for i in range(1, 6))
    anomaly = next(a for a in detect(session) if a.kind == "file-thrash")
    self.assertEqual(anomaly.lines, [1, 2, 3, 4, 5])

def test_file_thrash_negative(self) -> None:
    session = _session()
    session.files.extend(FileOp(f"/{i}.py", "edit", "", "t", line=i) for i in range(9))
    self.assertNotIn("file-thrash", _kinds(session))

def test_read_loop(self) -> None:
    session = _session()
    session.files.extend(FileOp("/a.py", "read", "", "t", line=i) for i in range(1, 5))
    anomaly = next(a for a in detect(session) if a.kind == "read-loop")
    self.assertEqual(anomaly.lines, [1, 2, 3, 4])
```

Also update the one remaining bare call, `test_clean_session_has_no_anomalies`
(`tests/test_classify.py:233`): `FileOp("/a.py", "read", "", "t", line=1)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse tests.test_classify -v`
Expected: `test_file_operations_carry_their_line_number` fails with `TypeError:
__init__() got an unexpected keyword argument 'line'`; the two updated `DetectorTest` tests fail
on `anomaly.lines == []`.

- [ ] **Step 3: Add `FileOp.line` and thread it through**

`sessionkit/parse.py` — add the field:

```python
@dataclass
class FileOp:
    """A read/write/edit against a path."""

    path: str
    op: str
    ts: str
    tool_use_id: str
    line: int = 0
```

`sessionkit/parse.py:_file_op` — pass it through:

```python
    def _file_op(self, call: ToolCall, raw_input: Any, ts: str) -> None:
        """Record a file operation when the tool is one of the file tools."""
        op = FILE_TOOLS.get(call.name)
        if not op or not isinstance(raw_input, dict):
            return
        for key in PATH_KEYS:
            value = raw_input.get(key)
            if isinstance(value, str) and value:
                self.out.files.append(FileOp(value, op, ts, call.tool_use_id, call.line))
                return
```

- [ ] **Step 4: Make `_path_churn` accumulate real lines**

`sessionkit/classify.py`:

```python
def _path_churn(session: ParsedSession, op: str, key: str, kind: str) -> list[Anomaly]:
    """Shared implementation for edit-thrash and read-loop detection."""
    lines: dict[str, list[int]] = {}
    for f in session.files:
        if f.op == op:
            lines.setdefault(f.path, []).append(f.line)
    limit = THRESHOLDS[key]
    return [Anomaly(kind, path, len(ls), ls) for path, ls in lines.items() if len(ls) >= limit]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse tests.test_classify -v`
Expected: all pass, including the previously-failing three.

- [ ] **Step 6: Run the full suite and commit**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all tests pass (no other test constructs `FileOp` positionally past 4 args, so nothing
else breaks).

```bash
git add sessionkit/parse.py sessionkit/classify.py tests/test_parse.py tests/test_classify.py
git commit -m "fix: file-thrash/read-loop findings carry real line numbers"
```

---

## Task 2: `sk index` shows `parent_sid`/`agent_type` for subagent rows

**Files:**
- Modify: `sessionkit/query.py:108-130` (`index_rows`)
- Modify: `sessionkit/cli.py:99-111` (`cmd_index`)
- Test: `tests/test_corpus.py`

**Interfaces:**
- Consumes: `ParsedSession.parent_sid` / `.agent_type` — already parsed (`parse.py:134-135`,
  `:437`), just not surfaced. No parser change needed.
- Produces: two new keys, `parent_sid` and `agent_type`, on every `index_rows` row (always
  present, so callers never branch on their absence); `cmd_index` renders them as extra columns
  only when subagents are in scope.

This directly answers `FIRSTRUN.md` §2's first suggestion: today finding a session's children
needs a fleet-wide grep by label text (steps 2–4 in that section) because nothing on `sk index`
says which parent a subagent belongs to.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus.py — new test in an existing or new class near IndexTest-style coverage;
# place it in CorpusTest or a small new IndexColumnsTest following the file's existing setUp
# pattern (CLAUDE_DIR/SESSIONKIT_ENV env patch, tempfile home)
def test_index_reports_parent_sid_and_agent_type_for_subagents(self) -> None:
    fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
    fx.write_subagent(self.home, [
        fx.user("explore"),
        fx.assistant([fx.tool_use("s1", "Grep", {"pattern": "TODO"})]),
        fx.tool_result("s1", "not found"),
    ], agent_id="eeee5555", agent_type="Explore")
    rows = query.index_rows(corpus.load(), query.Filter(subagents="only"))
    self.assertEqual(rows[0]["parent_sid"], fx.SID)
    self.assertEqual(rows[0]["agent_type"], "Explore")

    args = cli.build_parser().parse_args(["index", "--subagents", "only"])
    out = cli.cmd_index(corpus.load(), args)
    self.assertIn("parent_sid", out)
    self.assertIn("Explore", out)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus -v -k parent_sid`
Expected: `KeyError: 'parent_sid'`.

- [ ] **Step 3: Add the fields to `index_rows`**

`sessionkit/query.py`:

```python
def index_rows(corpus: Corpus, scope: Filter) -> list[Row]:
    rows = scope.apply(corpus)
    rows.sort(key=lambda e: e.session.ended_at or "", reverse=True)
    out: list[Row] = []
    for entry in rows:
        s = entry.session
        out.append({
            "sid": s.sid,
            "project_key": entry.project_key,
            "ended_at": s.ended_at,
            "turns": s.turns,
            "cost_usd": s.cost_usd,
            "end_state": s.end_state,
            "model": s.model,
            "label": s.title or s.first_prompt,
            "parent_sid": s.parent_sid,
            "agent_type": s.agent_type,
        })
    return out
```

- [ ] **Step 4: Render the extra columns conditionally**

`sessionkit/cli.py`:

```python
def cmd_index(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 1: one line per session."""
    rows = query.index_rows(corpus, _scope(args))
    report = Report(args.json, args.budget_kb or BUDGET_INDEX_KB)
    report.meta(sessions=len(rows), subagents=args.subagents)
    headers = ["sid", "project", "ended", "turns", "cost", "state", "model", "label"]
    show_lineage = args.subagents in ("include", "only")
    if show_lineage:
        headers += ["parent_sid", "agent_type"]
    table_rows = []
    for r in rows:
        row = [r["sid"][:8], r["project_key"], (r["ended_at"] or "")[:16], r["turns"],
               f"{r['cost_usd']:.2f}", r["end_state"], _short_model(r["model"]), r["label"] or ""]
        if show_lineage:
            row += [r["parent_sid"][:8] if r["parent_sid"] else "-", r["agent_type"] or "-"]
        table_rows.append(row)
    report.table(headers, table_rows, key="sessions")
    return report.render()
```

- [ ] **Step 5: Run the test to verify it passes, then the full suite**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus -v -k parent_sid`
Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all pass. (`--json` output already carried `parent_sid`/`agent_type` implicitly via
`dict(zip(headers, row))` in `render.py`, so JSON callers get the same fields with no separate
change.)

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_corpus.py
git commit -m "feat: sk index shows parent_sid/agent_type for subagent rows"
```

---

## Task 3: Foundation — exact dispatch→child resolution (`dispatch_edges`)

**Files:**
- Modify: `sessionkit/parse.py` (new `ParsedSession.dispatch_edges` field, new regex, one new
  line in `parse_file`'s per-line loop)
- Modify: `tests/fixtures.py` (new `task_notification()` builder)
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: nothing new — reads the raw JSONL line already in scope in `parse_file`'s loop.
- Produces: `ParsedSession.dispatch_edges: list[tuple[str, str]]` — `(tool_use_id, task_id)`
  pairs, in the order encountered. Task 4 (`sk children`) is the first consumer; `PLAN.md`
  Phase 5's `sk cost --subagents` is the second (Task 9 records that dependency so Phase 5 does
  not re-derive this).

**Why this is a foundation task, not part of Task 4.** `FIRSTRUN.md` §8 found that resolving a
dispatch to its child by anything *other than* this exact join is actively wrong, not just
approximate: pairing children to dispatches by sorting on completion time silently mispaired
"Implement Task 1" with a review session, because completions don't arrive in dispatch order
once retries or non-adjacent progress notifications happen. Both `sk children` and the future
`sk cost --subagents` need the identical exact edge; building it once here is what §2 of
`design-cross-cutting-work` calls "spec once" — the alternative is Phase 5 re-deriving the same
join, possibly wrong in the same way.

**What could not be verified here, stated plainly.** This container's own corpus (`sk doctor`:
10 sessions, no subagent dispatches) has no real `<task-notification>` record to check the exact
JSON field/record-type against. The regex below matches raw, decoded text for the tag pair
`FIRSTRUN.md` quotes verbatim — `<task-id>…</task-id>` immediately followed (across whitespace)
by `<tool-use-id>…</tool-use-id>` — and is deliberately **not** anchored to a record `type` or an
`isMeta` flag, so it is agnostic to which field carries the tags. This mirrors the precedent
already in this codebase for exactly this situation: `sk hooks`' hook-attribution join shipped
"validated against synthetic fixtures … not this machine's live corpus" (`PLAN.md` §7 Phase 2)
and was corrected against the real corpus once one with real hook failures was available. Task 9
carries the same caveat into `PLAN.md`.

- [ ] **Step 1: Write the failing test**

```python
# tests/fixtures.py — add near the bottom, after simple_session()
def task_notification(tool_use_id: str, task_id: str, ts: str = "2026-08-01T00:00:05Z"
                      ) -> dict[str, Any]:
    """A synthetic ``<task-notification>`` record, as a background Agent dispatch resolves.

    Field/record-type placement is unverified against a real corpus (no subagent dispatch exists
    in this container's own corpus) — see PLAN docs/superpowers/plans/2026-08-28-firstrun-fixes.md
    Task 3. The tag text itself is quoted verbatim from FIRSTRUN.md §8.
    """
    body = (f"Task finished. <task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n</task-notification>")
    return {"type": "user", "sessionId": SID, "cwd": CWD, "timestamp": ts,
            "uuid": f"tn-{tool_use_id}", "isMeta": True,
            "message": {"role": "user", "content": body}}
```

```python
# tests/test_parse.py — new test in class ParseTest
def test_task_notification_records_a_dispatch_edge(self) -> None:
    session = self.parse([
        fx.user("go"),
        fx.assistant([fx.tool_use("toolu_1", "Agent", {"description": "do work"})]),
        fx.task_notification("toolu_1", "childsid123"),
    ])
    self.assertEqual(session.dispatch_edges, [("toolu_1", "childsid123")])

def test_no_dispatch_edges_when_no_notification_present(self) -> None:
    session = self.parse(fx.simple_session())
    self.assertEqual(session.dispatch_edges, [])
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse -v -k dispatch_edge`
Expected: `AttributeError: 'ParsedSession' object has no attribute 'dispatch_edges'`.

- [ ] **Step 3: Add the field and the regex**

`sessionkit/parse.py` — add `import re` to the existing import block (alongside `hashlib`,
`json`), then:

```python
#: A background-agent dispatch resolves through a record carrying both the child's own sid
#: (<task-id>) and the tool_use_id of the Agent call that dispatched it (<tool-use-id>) — see
#: docs/superpowers/plans/2026-08-28-firstrun-fixes.md Task 3 for the exact join this feeds.
_TASK_NOTIF_RE = re.compile(
    r"<task-id>([^<]+)</task-id>\s*<tool-use-id>([^<]+)</tool-use-id>", re.S)
```

Add the field to `ParsedSession`:

```python
    dispatch_edges: list[tuple[str, str]] = field(default_factory=list)
```

Add a small flattener next to `block_text` (needed because the tags may sit inside any string
field of the record, and matching against real decoded text — not the raw JSON line — is what
lets `\s*` match a real newline instead of a literal backslash-n):

```python
def _flatten_strings(value: Any) -> str:
    """Every string value in a JSON-decoded structure, joined — enough to regex-scan a record
    without knowing which field holds the text of interest."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_strings(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_strings(v) for v in value)
    return ""
```

- [ ] **Step 4: Hook it into `parse_file`'s per-line loop**

`sessionkit/parse.py:parse_file` — after `parser.feed(line_no, rec)`, add a cheap-reject check
before doing any flattening, so the common case (no notification on this line) costs one
substring scan:

```python
            parser.feed(line_no, rec)
            if "<task-notification" in line:
                for task_id, tool_use_id in _TASK_NOTIF_RE.findall(_flatten_strings(rec)):
                    parser.out.dispatch_edges.append((tool_use_id.strip(), task_id.strip()))
```

- [ ] **Step 5: Run the tests to verify they pass, then the full suite**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse -v -k dispatch_edge`
Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add sessionkit/parse.py tests/fixtures.py tests/test_parse.py
git commit -m "feat: parse task-notification dispatch edges (tool_use_id -> child sid)"
```

---

## Task 4: `sk children <sid>`

**Files:**
- Modify: `sessionkit/query.py` (new `children_rows`)
- Modify: `sessionkit/cli.py` (new `cmd_children`, new `p_children` subparser, `COMMANDS` entry)
- Test: `tests/test_corpus.py`

**Interfaces:**
- Consumes: `Loaded.session.dispatch_edges` (Task 3), `Loaded.session.tools` (existing,
  `name == "Agent"` rows), `Corpus.sessions` (existing, to resolve a child by sid fleet-wide —
  children can live in a different project than their parent, per `FIRSTRUN.md` §6).
- Produces: `query.children_rows(entry: Loaded, corpus: Corpus) -> list[Row]`, each row carrying
  `line`, `child_sid`, `resolved: bool`, `agent_type`, `state`, `cost_usd`, `project`,
  `description`.

This is the second half of `FIRSTRUN.md` §2's suggestion, and it is what turns "ten-plus `sk`
calls, most of them guesses" into one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus.py — new class, following the file's existing setUp pattern
class ChildrenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_resolves_by_the_exact_join_not_dispatch_order(self) -> None:
        # Two dispatches; their notifications arrive in the OPPOSITE order, which would silently
        # mispair a positional/timestamp resolver (FIRSTRUN.md §8) but must not fool the real one.
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([
                fx.tool_use("d1", "Agent", {"description": "Implement Task 1"}),
                fx.tool_use("d2", "Agent", {"description": "Implement Task 2"}),
            ]),
            fx.task_notification("d2", "childtwo01"),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childone01",
                          agent_type="general-purpose")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childtwo01",
                          agent_type="general-purpose")
        corp = corpus.load()
        parent = next(e for e in corp.sessions if not e.session.is_subagent)
        rows = query.children_rows(parent, corp)
        by_line = {r["description"]: r["child_sid"] for r in rows}
        self.assertEqual(by_line["Implement Task 1"][:10], "childone01")
        self.assertEqual(by_line["Implement Task 2"][:10], "childtwo01")

    def test_unresolved_dispatch_is_reported_not_guessed(self) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Implement Task 1"})]),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        parent = next(e for e in corp.sessions if not e.session.is_subagent)
        rows = query.children_rows(parent, corp)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["resolved"])
        self.assertEqual(rows[0]["child_sid"], "")

        out = cli.cmd_children(corp, self._args("children", parent.session.sid))
        self.assertIn("1 dispatch(es)", out)
        self.assertIn("unresolved", out)

    def test_duplicate_task_notifications_do_not_double_count(self) -> None:
        # A single task can notify twice (progress + completion, per FIRSTRUN.md §8's
        # `a3c7bebc` example). Duplicate (tool_use_id, task_id) edges must collapse to one row.
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Implement Task 1"})]),
            fx.task_notification("d1", "childone01"),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childone01",
                          agent_type="general-purpose")
        corp = corpus.load()
        parent = next(e for e in corp.sessions if not e.session.is_subagent)
        rows = query.children_rows(parent, corp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["child_sid"][:10], "childone01")
        self.assertTrue(rows[0]["resolved"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus -v -k Children`
Expected: `AttributeError: module 'sessionkit.query' has no attribute 'children_rows'`.

- [ ] **Step 3: Implement `children_rows`**

`sessionkit/query.py` — add near the `commands` section:

```python
# --- children ---------------------------------------------------------------------------

def children_rows(entry: Loaded, corpus: Corpus) -> list[Row]:
    """Every ``Agent`` dispatch from one session, resolved to its child via the exact
    task-notification join (``parse.py``'s ``dispatch_edges``) — never by timestamp or dispatch
    order, which FIRSTRUN.md §8 found silently mispairs retries and non-adjacent completions.

    A dispatch with no matching notification is reported as unresolved rather than guessed.
    """
    edges = dict(entry.session.dispatch_edges)
    by_sid = {c.session.sid: c for c in corpus.sessions if c.session.is_subagent}
    out: list[Row] = []
    for tool in entry.session.tools:
        if tool.name != "Agent":
            continue
        task_id = edges.get(tool.tool_use_id, "")
        child = by_sid.get(task_id) if task_id else None
        out.append({
            "line": tool.line,
            "description": _agent_description(tool),
            "child_sid": child.session.sid if child else "",
            "resolved": child is not None,
            "agent_type": child.session.agent_type if child else "",
            "state": child.session.end_state if child else "",
            "cost_usd": child.session.cost_usd if child else 0.0,
            "project": child.project_key if child else "",
        })
    out.sort(key=lambda r: r["line"])
    return out


def _agent_description(tool: ToolCall) -> str:
    """Best-effort dispatch description from an ``Agent`` call's input."""
    try:
        parsed = json.loads(tool.input_preview)
    except (ValueError, TypeError):
        return tool.input_preview
    if isinstance(parsed, dict):
        return str(parsed.get("description") or parsed.get("prompt") or tool.input_preview)
    return tool.input_preview
```

- [ ] **Step 4: Wire up the CLI command**

`sessionkit/cli.py` — add the handler near `cmd_forensics`:

```python
def cmd_children(corpus: Corpus, args: argparse.Namespace) -> str:
    """Every Agent dispatch from one session, resolved to its child sid, state and cost.

    Collapses what FIRSTRUN.md §2 needed ten-plus `sk` calls and a label-text guess to find.
    """
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    rows = query.children_rows(entry, corpus)
    unresolved = sum(1 for r in rows if not r["resolved"])

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB)
    report.meta(sid=entry.session.sid, dispatches=len(rows), unresolved=unresolved)
    if unresolved:
        report.text(f"{unresolved} dispatch(es) have no task-notification match in this "
                    "transcript — reported as unresolved rather than guessed by order.")
    report.section("Children")
    report.table(
        ["line", "child_sid", "resolved", "agent_type", "state", "cost", "project", "dispatch"],
        [[r["line"], r["child_sid"][:8] if r["child_sid"] else "-",
          "yes" if r["resolved"] else "no", r["agent_type"] or "-", r["state"] or "-",
          f"{r['cost_usd']:.2f}", r["project"] or "-", r["description"]] for r in rows],
        key="children",
    )
    return report.render()
```

Add the subparser, next to `p_forensics`:

```python
    p_children = sub.add_parser("children", parents=[common],
                                help="Agent dispatches from one session, resolved to child sid")
    p_children.add_argument("sid", help="session id or unique prefix")
```

Register it:

```python
COMMANDS = {"doctor": cmd_doctor, "index": cmd_index, "show": cmd_show, "errors": cmd_errors,
            "commands": cmd_commands, "hooks": cmd_hooks, "forensics": cmd_forensics,
            "children": cmd_children}
```

- [ ] **Step 5: Run the tests to verify they pass, then the full suite**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus -v -k Children`
Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_corpus.py
git commit -m "feat: add sk children, resolving Agent dispatches to their child session"
```

---

## Task 5: `--full` on `sk show` — full-fidelity re-read, not a bigger cap

**Files:**
- Modify: `sessionkit/parse.py` (`ToolCall.result_line`, `_tool_results` signature, `read_line`)
- Modify: `sessionkit/query.py` (`tool_rows`, `error_rows`, `message_rows` gain `full=False`)
- Modify: `sessionkit/cli.py` (`p_show` gets `--full`, `cmd_show`/`_show_*` pass it through)
- Test: `tests/test_parse.py`, `tests/test_corpus.py`

**Interfaces:**
- Consumes: `entry.session.path` (existing), `ToolCall.line`/new `result_line`, `Message.line`
  (existing).
- Produces: `parse.read_line(path: str, line_no: int) -> dict[str, Any] | None`; `query.tool_rows`
  / `error_rows` / `message_rows` accept `full: bool = False` and, when set, substitute the
  re-read, redacted, **un**truncated text for the stored preview.

**Root cause, not just the symptom** (`FIRSTRUN.md` §4): `sk commands --full` already exists and
bypasses render-layer truncation, but `input_preview` itself is built by truncating an
**already-JSON-serialized** string at a raw character offset (`parse.py:296`,
`preview(json.dumps(raw_input, default=str), INPUT_PREVIEW)`) — the *only* copy kept in memory,
per that line's own comment. Raising `INPUT_PREVIEW` (already done once, per `PLAN.md` Phase 2)
only moves the cliff; a 19,771-character prompt still gets cut mid-escape-sequence at 2000 chars,
and re-parsing that cut string as JSON throws exactly the `Unterminated string` error
`FIRSTRUN.md` hit. `--full` on `sk show` fixes this at the source: re-read the one transcript
line the row came from and use the real content, never the capped preview. `sk commands --full`
stays as-is — it aggregates across many rows with no single source line to re-read per exemplar,
so it keeps disabling render-layer truncation only, as today; the full-fidelity path applies
where `SPEC.md` §4 asks for it, "for the one row being investigated."

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parse.py — new test in class ParseTest
def test_result_line_is_recorded_on_the_tool_call(self) -> None:
    session = self.parse([
        fx.user("go"),
        fx.assistant([fx.tool_use("t1", "Bash", {"command": "ls"})]),
        fx.tool_result("t1", "ok"),
    ])
    self.assertEqual(session.tools[0].result_line, 3)

# tests/test_parse.py — new test class
class ReadLineTest(unittest.TestCase):
    def test_reads_exact_line_and_none_past_the_end(self) -> None:
        path = fx.write(Path(tempfile.mkdtemp()), fx.simple_session(), name="aaaa1111.jsonl")
        rec = read_line(str(path), 1)
        self.assertEqual(rec["type"], "user")
        self.assertIsNone(read_line(str(path), 999))
```

(Add `import tempfile` and `from sessionkit.parse import read_line` to `test_parse.py`'s imports
if not already present — check the existing import block first.)

```python
# tests/test_corpus.py — new class
class ShowFullTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_full_recovers_a_tool_input_past_the_preview_cap(self) -> None:
        long_arg = "x" * 3000  # past INPUT_PREVIEW=2000
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": long_arg})]),
            fx.tool_result("t1", "ok"),
        ], name="aaaa1111.jsonl")
        loaded = corpus.load()
        sid = loaded.sessions[0].session.sid
        capped = cli.cmd_show(loaded, self._args("show", sid, "--mode", "tools"))
        full = cli.cmd_show(loaded, self._args("show", sid, "--mode", "tools", "--full"))
        self.assertNotIn(long_arg, capped)
        self.assertIn(long_arg, full)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse tests.test_corpus -v -k "result_line or ReadLine or FullTest"`
Expected: `AttributeError`/`TypeError` (`result_line` doesn't exist; `read_line` doesn't exist;
`--full` is not a recognised argument on `show`).

- [ ] **Step 3: Add `result_line` and `read_line` to `parse.py`**

Add the field. The current `ToolCall` has `tool_use_id`, `line`, `name`, `ts` with no
defaults, then defaulted fields starting at `result_ts`. Python forbids a defaulted field
before a non-defaulted one, so `result_line: int = 0` goes at the head of the defaulted
block — immediately after `ts`, immediately before `result_ts`:

```python
@dataclass
class ToolCall:
    """One tool invocation, paired with its result when the result arrived."""

    tool_use_id: str
    line: int
    name: str
    ts: str
    result_line: int = 0
    result_ts: str = ""
    dur_ms: int | None = None
    is_error: bool = False
    err_class: str = ""
    err_detail: str = ""
    input_digest: str = ""
    input_preview: str = ""
    output_preview: str = ""
    out_bytes: int = 0
```

(Re-check `tests/test_classify.py`'s `_call()` helper after this edit — it builds `ToolCall`
by keyword already, so it is unaffected.)

Update `_tool_results` to take and record the line number:

```python
    def _tool_results(self, line_no: int, ts: str, content: Any) -> int:
        """Pair ``tool_result`` blocks with their pending calls. Returns how many were found."""
        if not isinstance(content, list):
            return 0
        found = 0
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            found += 1
            call = self.pending.pop(str(block.get("tool_use_id") or ""), None)
            if call is None:
                continue
            body = block_text(block.get("content"))
            call.result_ts = ts
            call.result_line = line_no
            call.dur_ms = _delta_ms(call.ts, ts)
            call.is_error = bool(block.get("is_error"))
            call.out_bytes = len(body)
            call.output_preview = preview(body, OUTPUT_PREVIEW)
        return found
```

Update its one call site in `_user`:

```python
        results = self._tool_results(line_no, ts, content)
```

Add `read_line` (module-level function, near `_agent_type` at the bottom of the file):

```python
def read_line(path: str, line_no: int) -> dict[str, Any] | None:
    """Re-read one exact transcript line, bypassing every in-memory preview cap.

    ``--full`` needs the original record — ``input_preview``/``output_preview``/``preview`` are
    the only copies kept in memory, each capped, and a preview built from an already-serialised
    JSON string can be cut mid-escape (docs/superpowers/plans/2026-08-28-firstrun-fixes.md
    Task 5). Re-reading the source line is the only way to recover the true text.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle, start=1):
                if i == line_no:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        return None
                    return rec if isinstance(rec, dict) else None
    except OSError:
        return None
    return None
```

- [ ] **Step 4: Add the full-fidelity row builders to `query.py`**

```python
from sessionkit.parse import ToolCall, block_text, read_line
from sessionkit.redact import redact
```

(merge into the existing `from sessionkit.parse import ToolCall` line; add the `redact` import
alongside the existing `from sessionkit.classify import ANOMALY_HINTS, signature` line)

```python
def _full_input(entry: Loaded, call: ToolCall) -> str:
    """Re-read a tool call's true input from its source line (see parse.read_line)."""
    rec = read_line(entry.session.path, call.line)
    if rec is not None:
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("id") == call.tool_use_id):
                    return redact(json.dumps(block.get("input"), default=str))
    return call.input_preview


def _full_output(entry: Loaded, call: ToolCall) -> str:
    """Re-read a tool result's true output from its source line."""
    if not call.result_line:
        return call.output_preview
    rec = read_line(entry.session.path, call.result_line)
    if rec is not None:
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == call.tool_use_id):
                    return redact(block_text(block.get("content")))
    return call.output_preview


def _full_message(entry: Loaded, message: Any) -> str:
    """Re-read a message's true text from its source line."""
    rec = read_line(entry.session.path, message.line)
    if rec is None:
        return message.preview
    content = (rec.get("message") or {}).get("content")
    return redact(block_text(content)) if content is not None else message.preview
```

Update the three row builders to accept and use `full`:

```python
def tool_rows(entry: Loaded, full: bool = False) -> list[Row]:
    """Every tool call in the session, in line order."""
    return [{"line": t.line, "name": t.name, "dur_ms": t.dur_ms, "err_class": t.err_class,
             "input_preview": _full_input(entry, t) if full else t.input_preview}
            for t in entry.session.tools]


def error_rows(entry: Loaded, full: bool = False) -> list[Row]:
    """Only the failing tool calls."""
    return [{"line": t.line, "name": t.name, "err_class": t.err_class,
             "output_preview": _full_output(entry, t) if full else t.output_preview}
            for t in entry.session.tools if is_failure(t)]


def message_rows(entry: Loaded, lo: int, hi: int, full: bool = False) -> list[Row]:
    """Messages within an inclusive line range."""
    return [{"line": m.line, "role": m.role, "text_len": m.text_len,
             "preview": _full_message(entry, m) if full else m.preview}
            for m in entry.session.messages if lo <= m.line <= hi]
```

- [ ] **Step 5: Wire `--full` through the CLI**

`sessionkit/cli.py` — add the flag next to `--mode`/`--range`:

```python
    p_show.add_argument("--full", action="store_true",
                        help="re-read tool/message text from source for full fidelity, past "
                             "the in-memory preview cap (JSON never truncates a cell either way)")
```

Pass it to the report and the three handlers that use previews:

```python
def cmd_show(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 3: a surgical excerpt of one session."""
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    session = entry.session

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB, full=args.full)
    ...  # meta()/end_reason unchanged
    handlers = {"timeline": _show_timeline, "messages": _show_messages,
                "tools": _show_tools, "errors": _show_errors}
    handlers.get(args.mode, _show_summary)(entry, args, report)
    return report.render()
```

```python
def _show_messages(entry: corpus_mod.Loaded, args: argparse.Namespace,
                   report: Report) -> None:
    """A line-numbered range of messages."""
    lo, hi = _range(args.range)
    report.section(f"Messages {lo}:{hi}")
    report.table(["line", "role", "chars", "preview"],
                 [[r["line"], r["role"], r["text_len"], r["preview"]]
                  for r in query.message_rows(entry, lo, hi, full=args.full)],
                 key="messages")


def _show_tools(entry: corpus_mod.Loaded, args: argparse.Namespace, report: Report) -> None:
    """Every tool call in the session."""
    report.section("Tool calls")
    report.table(["line", "tool", "ms", "err", "input"],
                 [[r["line"], r["name"], r["dur_ms"], r["err_class"] or "",
                   r["input_preview"]] for r in query.tool_rows(entry, full=args.full)],
                 key="tools")


def _show_errors(entry: corpus_mod.Loaded, args: argparse.Namespace, report: Report) -> None:
    """Only the failing tool calls, with their fix hints."""
    report.section("Errors")
    report.table(["line", "tool", "class", "fix", "detail"],
                 [[r["line"], r["name"], r["err_class"],
                   classify_error(r["output_preview"])[1],
                   r["output_preview"] or ""] for r in query.error_rows(entry, full=args.full)],
                 key="errors")
```

(These three handlers previously took `_args`/an unused second parameter — rename to `args` since
it is now used, matching the other handlers' style.)

- [ ] **Step 6: Run the tests to verify they pass, then the full suite**

Run: `PYTHONPATH=. python3 -m unittest tests.test_parse tests.test_corpus -v -k "result_line or ReadLine or FullTest"`
Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add sessionkit/parse.py sessionkit/query.py sessionkit/cli.py tests/test_parse.py tests/test_corpus.py
git commit -m "fix: sk show --full re-reads source lines instead of the capped preview"
```

---

## Task 6: `--since` accepts an absolute date

**Files:**
- Modify: `sessionkit/cli.py:21-45` (`since_cutoff`)
- Test: a new small test module or an existing CLI-adjacent test file — add
  `tests/test_cli.py` if none of the existing files already cover `cli.since_cutoff` (check
  first with `grep -rn since_cutoff tests/`).

**Interfaces:**
- Consumes: nothing new.
- Produces: `since_cutoff` now accepts `YYYY-MM-DD` and `YYYY-MM-DDTHH:MM[:SS]` in addition to the
  existing `7d`/`12h`/`2w` relative form. Return type and every caller are unchanged — this is a
  pure input-format widening.

`FIRSTRUN.md` §5: "wanting 'since this specific date' rather than a relative window is the normal
way to scope a known-date incident," and this is already a `PLAN.md` §4/§5.1 roadmap item —
implementing the narrowest, already-agreed-upon slice of it.

- [ ] **Step 1: Write the failing test**

```bash
grep -rn "since_cutoff" /workspace/tests/
```

If no test file covers it yet, create `tests/test_cli.py`:

```python
"""cli.py helpers not already covered by an integration test in test_corpus.py."""

from __future__ import annotations

import unittest

from sessionkit import cli


class SinceCutoffTest(unittest.TestCase):
    def test_relative_window_unchanged(self) -> None:
        self.assertTrue(cli.since_cutoff("7d").endswith("Z"))

    def test_absolute_date(self) -> None:
        self.assertEqual(cli.since_cutoff("2026-08-17"), "2026-08-17T00:00:00Z")

    def test_absolute_datetime(self) -> None:
        self.assertEqual(cli.since_cutoff("2026-08-17T14:30"), "2026-08-17T14:30:00Z")

    def test_empty_is_no_cutoff(self) -> None:
        self.assertEqual(cli.since_cutoff(None), "")

    def test_unrecognised_value_still_raises(self) -> None:
        with self.assertRaises(SystemExit):
            cli.since_cutoff("not-a-date")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_cli -v`
Expected: `test_absolute_date`/`test_absolute_datetime` fail with `SystemExit` (the current
function rejects anything not matching `_DURATION`).

- [ ] **Step 3: Widen `since_cutoff`**

`sessionkit/cli.py`:

```python
_DURATION = re.compile(r"^(\d+)\s*([hdw])$", re.I)
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
_ABSOLUTE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")


def since_cutoff(value: str | None) -> str:
    """Convert a ``7d``/``12h``/``2w`` window, or an absolute ``YYYY-MM-DD[THH:MM[:SS]]`` date,
    into an ISO-8601 cutoff timestamp.

    Args:
        value: A relative duration, an absolute date/datetime, or ``None`` for no cutoff.

    Returns:
        An ISO timestamp string, or ``""`` when no cutoff applies.

    Raises:
        SystemExit: If neither form parses — a silently-ignored ``--since`` would make a partial
            report look complete.
    """
    if not value:
        return ""
    value = value.strip()
    match = _DURATION.match(value)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        cutoff = datetime.now(timezone.utc) - timedelta(**{_UNITS[unit]: amount})
        return cutoff.isoformat().replace("+00:00", "Z")
    if _ABSOLUTE.match(value):
        iso = value if "T" in value else f"{value}T00:00:00"
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    raise SystemExit(f"unrecognised --since value {value!r}; expected e.g. 7d, 12h, 2w, or an "
                     "absolute date/time like 2026-08-17 or 2026-08-17T14:30")
```

- [ ] **Step 4: Run the tests to verify they pass, then the full suite**

Run: `PYTHONPATH=. python3 -m unittest tests.test_cli -v`
Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add sessionkit/cli.py tests/test_cli.py
git commit -m "feat: --since accepts an absolute date in addition to relative windows"
```

---

## Task 7: `--label-contains` on `sk index`

**Files:**
- Modify: `sessionkit/query.py:37-66` (`Filter`)
- Modify: `sessionkit/cli.py:48-56,397-400` (`_scope`, `p_index`)
- Test: `tests/test_corpus.py`

**Interfaces:**
- Consumes: `entry.session.title` / `.first_prompt` (existing).
- Produces: `Filter.label_contains: str = ""`; when set, `Filter.matches` requires a
  case-insensitive substring match against the session's label. Every existing `Filter(...)`
  construction is unaffected (new field defaults to `""`, i.e. no filtering).

`FIRSTRUN.md` §5: without this, finding a session by name means scanning hundreds of rows by eye
("this is what step 4 in §2 stood in for, badly").

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus.py — add to an existing index-focused test class, or a small new one
def test_label_contains_filters_case_insensitively(self) -> None:
    fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
    # simple_session()'s first prompt is "add a feature"
    matches = query.index_rows(corpus.load(), query.Filter(label_contains="FEATURE"))
    self.assertEqual(len(matches), 1)
    no_matches = query.index_rows(corpus.load(), query.Filter(label_contains="nonexistent"))
    self.assertEqual(no_matches, [])
```

(Place this in whichever existing class already has a `home`/env-patched `setUp` for `index_rows`
— check `CorpusTest`/`QueryTest` first via `grep -n "index_rows" tests/test_corpus.py` rather than
adding a fifth near-duplicate `setUp`.)

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus -v -k label_contains`
Expected: `TypeError: Filter.__init__() got an unexpected keyword argument 'label_contains'`.

- [ ] **Step 3: Add the field to `Filter`**

`sessionkit/query.py`:

```python
@dataclass
class Filter:
    """The shared scope flags: ``--since``/``--project``/``--source``/``--state``/
    ``--label-contains``."""

    cutoff: str = ""
    project: str = ""
    source: str = ""
    state: str = ""
    subagents: str = "include"
    label_contains: str = ""

    def matches(self, entry: Loaded) -> bool:
        """Whether a session is in scope."""
        session = entry.session
        if self.cutoff and not (session.ended_at or "") >= self.cutoff:
            return False
        if self.project and entry.project_key != self.project.lower():
            return False
        if self.source and session.source_id != self.source:
            return False
        if self.state and session.end_state != self.state:
            return False
        if self.subagents == "exclude" and session.is_subagent:
            return False
        if self.subagents == "only" and not session.is_subagent:
            return False
        if self.label_contains:
            label = (session.title or session.first_prompt or "").lower()
            if self.label_contains.lower() not in label:
                return False
        return True
```

- [ ] **Step 4: Wire the flag into `sk index`**

`sessionkit/cli.py:_scope`:

```python
def _scope(args: argparse.Namespace) -> query.Filter:
    """Build the shared session filter from the common scope flags."""
    return query.Filter(
        cutoff=since_cutoff(getattr(args, "since", None)),
        project=getattr(args, "project", None) or "",
        source=getattr(args, "source", None) or "",
        state=getattr(args, "state", None) or "",
        subagents=getattr(args, "subagents", "include"),
        label_contains=getattr(args, "label_contains", None) or "",
    )
```

`sessionkit/cli.py:build_parser`, on `p_index`:

```python
    p_index = sub.add_parser("index", parents=[common, scoped],
                             help="one line per session (layer 1)")
    p_index.add_argument("--state", help="filter by end_state, e.g. interrupted-tool")
    p_index.add_argument("--label-contains",
                         help="only sessions whose title/label contains this text "
                              "(case-insensitive)")
    _subagents_arg(p_index, "exclude")
```

- [ ] **Step 5: Run the tests to verify they pass, then the full suite**

Run: `PYTHONPATH=. python3 -m unittest tests.test_corpus -v -k label_contains`
Run: `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add sessionkit/query.py sessionkit/cli.py tests/test_corpus.py
git commit -m "feat: sk index --label-contains filters sessions by title/label substring"
```

---

## Task 8: `session-forensics` skill — content fixes from the dogfood run

**Files:**
- Modify: `skills/session-forensics/SKILL.md`

**Interfaces:** none — documentation only, no code path changes. Depends on Task 4 (`sk
children` must exist before the skill can point to it).

`FIRSTRUN.md` §6's three suggestions, minus the `file-thrash`/`read-loop` caveat (moot — Task 1
fixes the underlying bug in this same plan, so there is nothing left to warn about).

- [ ] **Step 1: Add the orchestrator/subagent subsection**

In `skills/session-forensics/SKILL.md`, after the "### 1. Identify the session" section, insert:

```markdown
### 1a. A killed or orphaned subagent — find it from its parent

If a finding names `agent-kill` or `orphan-subagent`, the child transcript is a **separate**
session, filtered by **its own** project — which is the worktree/cwd it ran in, not the parent's
project name. Resolve it directly rather than guessing the project:

```bash
$SK children <parent-sid>     # every Agent dispatch from this session, resolved to its child sid
```

A dispatch reported as `resolved: no` has no `<task-notification>` match in the parent transcript
— treat that as "could not be resolved," not as "no child exists."
```

- [ ] **Step 2: Distinguish `agent-kill` from `orphan-subagent`**

In the "### 2. Findings and timeline" bullet list, replace:

```markdown
- **Findings** — every detected anomaly (repeat calls, file thrash, read loops, error cascades,
  hook ping-pong, killed/orphaned subagents, compaction churn, stalls, retried-after-rejection),
  each with a line-anchored prevention hint. A session with no findings is a real, positive
  result — say so plainly rather than padding the report.
```

with:

```markdown
- **Findings** — every detected anomaly, each with a line-anchored prevention hint. A session
  with no findings is a real, positive result — say so plainly rather than padding the report.
  Two subagent findings look similar but mean different things: `agent-kill` is user-initiated
  (someone stopped it); `orphan-subagent` means it was dispatched and never returned at all,
  which usually means it stalled or the parent moved on without waiting. Name which one fired —
  "you pulled the plug" and "it vanished" call for different follow-ups.
```

- [ ] **Step 3: Commit**

```bash
git add skills/session-forensics/SKILL.md
git commit -m "docs: session-forensics — point killed/orphaned-subagent findings at sk children"
```

---

## Task 9: `PLAN.md` amendments

**Files:**
- Modify: `PLAN.md`

**Interfaces:** none — documentation only. Per `SPEC.md` §6, only facts `PLAN.md` already owns
(command/skill scope, phase acceptance criteria) are edited here.

- [ ] **Step 1: Add `children` to the shipped command list**

In `PLAN.md` §5.1, change:

```markdown
**Shipped:** `doctor`, `index`, `show`, `errors`, `commands`, `hooks`, `forensics`.
```

to:

```markdown
**Shipped:** `doctor`, `index`, `show`, `errors`, `commands`, `hooks`, `forensics`, `children`.
```

- [ ] **Step 2: Record the dispatch-edge join as Phase 5's `--subagents` mechanism**

In `PLAN.md` Phase 5's acceptance bullet for `--subagents` (currently: *"`--subagents` compares
subagent cost against parent cost, **states the sample size** so a thin result isn't read as a
conclusion, and applies the wasted-dispatch test..."*), append a new bullet immediately after it:

```markdown
- **The parent↔child edge is `parse.py`'s `dispatch_edges`** (built for `sk children`, Phases
  0–2 addendum — see `docs/superpowers/plans/2026-08-28-firstrun-fixes.md` Task 3), joining each
  `Agent` `ToolCall.tool_use_id` to its child's sid via the `<task-notification>` record. Do not
  re-derive this by sorting children on completion time and zipping against dispatch order — that
  approach silently mispairs retries and non-adjacent completions (`FIRSTRUN.md` §8, confirmed:
  a wrong pairing put one task's cost on another task's row). Flag a non-`complete` child
  (`killed`, `interrupted-user`, …) as **sunk cost** rather than folding it into the parent's
  total undifferentiated — real spend with no surviving output is a distinct line, not noise.
```

- [ ] **Step 3: Commit**

```bash
git add PLAN.md
git commit -m "docs: record sk children and the dispatch-edge join in PLAN.md"
```

---

## Self-Review

**Spec coverage against `FIRSTRUN.md`:**
- §2 (navigational gap) → Tasks 2, 3, 4.
- §3 (`file-thrash`/`read-loop` line numbers) → Task 1.
- §4 (`--json` truncation / cell fidelity) → Task 5.
- §5 (`--since` absolute date, `--label-contains`) → Tasks 6, 7.
- §6 (skill content) → Task 8.
- §8 (task-notification join, sunk-cost framing) → Task 3 (join, built now) + Task 9 (Phase 5
  acceptance amendment; the join itself is *not* re-implemented inside `sk cost` here, because
  `sk cost` doesn't exist yet — that is Phase 5, out of scope for this plan).
- §1 (what worked) and §7 (other skills to run on this incident) are observations, not gaps —
  no task corresponds to them, correctly.

**Placeholder scan:** no TBD/"add appropriate handling"/unshown code — every step has runnable
code and an exact command.

**Type consistency check:** `FileOp(path, op, ts, tool_use_id, line=0)` (Task 1) is used
identically in Task 3's fixtures indirectly via `ToolCall`/`_file_op`, not `FileOp` directly, so
no cross-task drift. `ToolCall.result_line` (Task 5) and `dispatch_edges` (Task 3) are both
additive fields with defaults — no existing positional `ToolCall(...)` construction breaks
(`tests/test_classify.py`'s `_call()` helper builds `ToolCall` by keyword, confirmed above).
`query.children_rows(entry, corpus)` (Task 4) and `query.tool_rows(entry, full=False)` (Task 5)
are both consumed only by `cli.py` handlers introduced/edited in the same task — no stale
signature elsewhere.

**Task ordering:** Task 4 depends on Task 3 (needs `dispatch_edges`); Task 8 depends on **both**
Task 4 (references `sk children`) and Task 1 (removes the `file-thrash`/blank-`lines` caveat that
Task 1 makes obsolete). Tasks 1, 2, 6, 7 are independent of everything else and of each other. If
running subagent-driven-development, Tasks 1/2/6/7 can run in parallel; 3→4 must run in order;
Task 8 runs after 1 and 4 are both merged; Task 9 should run last (references `children` as
shipped and cites Task 3's file path).

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-28-firstrun-fixes.md`.** Two
execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between
tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution
with checkpoints.

**Which approach?**
