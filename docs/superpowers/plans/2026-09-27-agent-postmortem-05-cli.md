# Checkpoint 5: CLI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this checkpoint task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Part of:** the agent post-mortem plan. Tracker: `2026-09-27-agent-postmortem-00-tracker.md`. Read its **Global Constraints**, **Spec deviations** and **Review Focus** before starting; they apply to every task here.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**Goal:** `sk forensics` as the post-mortem report; `sk findings` with rates; `errors --group-by model|harness`; `index --finding`.

**Starts from:** Checkpoint 4 is complete: its tasks are ticked in the tracker and the full suite passes.

**Done when:** every task below is committed; `PYTHONPATH=. python3 -m unittest discover -s tests -t .` passes; and the tracker marks each task done with its commit.

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
