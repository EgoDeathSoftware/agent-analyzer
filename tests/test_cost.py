"""Tests for sk cost's query-layer aggregations (PLAN.md §7 Phase 5)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionkit import corpus, query
from tests import fixtures as fx


class CostQueryTestBase(unittest.TestCase):
    """Shared temp-corpus scaffolding, matching CorpusTest in test_corpus.py."""

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


class ToolResultSizesTest(CostQueryTestBase):
    def test_totals_avg_max_from_message_content_side(self) -> None:
        fx.write(self.home, [
            fx.user("read some files"),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/a.py"})]),
            fx.tool_result("t1", "x" * 100),
            fx.assistant([fx.tool_use("t2", "Read", {"file_path": "/b.py"})],
                        ts="2026-08-01T00:00:04Z"),
            fx.tool_result("t2", "y" * 300, ts="2026-08-01T00:00:05Z"),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        rows = query.tool_result_sizes(corp, query.Filter())
        read_row = next(r for r in rows if r["tool"] == "Read")
        self.assertEqual(read_row["n"], 2)
        self.assertEqual(read_row["total"], 400)
        self.assertEqual(read_row["max"], 300)
        self.assertAlmostEqual(read_row["avg"], 200.0)

    def test_pending_call_with_no_result_is_not_counted(self) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "sleep 100"})]),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        rows = query.tool_result_sizes(corp, query.Filter())
        self.assertEqual(rows, [])


class ToolCostRowsTest(CostQueryTestBase):
    def test_splits_message_cost_evenly_across_tool_use_blocks_on_same_line(self) -> None:
        # One assistant turn dispatching two tools at once — same shape parser.ts splits.
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([
                fx.tool_use("t1", "Read", {"file_path": "/a.py"}),
                fx.tool_use("t2", "Bash", {"command": "ls"}),
            ], usage={"input_tokens": 1_000_000, "output_tokens": 0}, model="claude-opus-5"),
            fx.tool_result("t1", "a"), fx.tool_result("t2", "b"),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        entry = corp.sessions[0]
        rows = {r["tool"]: r for r in query.tool_cost_rows(entry)}
        # 1e6 input tokens @ $5/M = $5.00, split evenly across 2 tool_use blocks -> $2.50 each.
        self.assertAlmostEqual(rows["Read"]["cost_usd"], 2.50)
        self.assertAlmostEqual(rows["Bash"]["cost_usd"], 2.50)

    def test_session_cost_summary_tool_plus_conversation_equals_total(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        entry = corp.sessions[0]
        summary = query.session_cost_summary(entry)
        self.assertAlmostEqual(summary["tool_cost"] + summary["conversation_cost"],
                               summary["cost_usd"])
        self.assertAlmostEqual(summary["cost_usd"], entry.session.cost_usd)


class BloatFindingsTest(CostQueryTestBase):
    def test_oversized_tool_rows_flags_average_above_threshold(self) -> None:
        fx.write(self.home, [
            fx.user("read"),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/a.py"})]),
            fx.tool_result("t1", "x" * (query.BLOAT_AVG_BYTES + 1)),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        rows = query.oversized_tool_rows(corp, query.Filter())
        self.assertEqual([r["tool"] for r in rows], ["Read"])

    def test_oversized_tool_rows_excludes_small_results(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        self.assertEqual(query.oversized_tool_rows(corp, query.Filter()), [])

    def test_truncation_notice_count(self) -> None:
        fx.write(self.home, [
            fx.user("read"),
            fx.attachment("read_truncation_notice"),
            fx.attachment("read_truncation_notice", ts="2026-08-01T00:00:04Z"),
            fx.attachment("hook_success", ts="2026-08-01T00:00:05Z"),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        self.assertEqual(query.truncation_notice_count(corp, query.Filter()), 2)

    def test_unbounded_bash_rows_flags_large_bash_output_only(self) -> None:
        fx.write(self.home, [
            fx.user("run"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "cat bigfile"})]),
            fx.tool_result("t1", "x" * (query.BLOAT_AVG_BYTES + 1)),
            fx.assistant([fx.tool_use("t2", "Read", {"file_path": "/a.py"})],
                        ts="2026-08-01T00:00:04Z"),
            fx.tool_result("t2", "y" * (query.BLOAT_AVG_BYTES + 1), ts="2026-08-01T00:00:05Z"),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        rows = query.unbounded_bash_rows(corp, query.Filter())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bytes"], query.BLOAT_AVG_BYTES + 1)

    def test_repeat_read_rows_sums_bytes_after_the_first_read(self) -> None:
        records = [fx.user("read a lot")]
        for i in range(5):
            records.append(fx.assistant([fx.tool_use(f"t{i}", "Read", {"file_path": "/a.py"})],
                                        ts=f"2026-08-01T00:0{i}:00Z"))
            records.append(fx.tool_result(f"t{i}", "x" * 50, ts=f"2026-08-01T00:0{i}:01Z"))
        fx.write(self.home, records, name="aaaa1111.jsonl")
        corp = corpus.load()
        rows = query.repeat_read_rows(corp, query.Filter())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reads"], 5)
        self.assertEqual(rows[0]["wasted_bytes"], 200)  # 4 re-reads x 50 bytes

    def test_repeat_read_rows_ignores_a_co_located_tool_call_on_the_same_line(self) -> None:
        """A Bash call sharing an assistant line with a Read must not contribute its bytes
        to the read-loop's wasted_bytes (see final-review-report.md C1)."""
        records = [fx.user("read a lot")]
        for i in range(5):
            records.append(fx.assistant([
                fx.tool_use(f"r{i}", "Read", {"file_path": "/a.py"}),
                fx.tool_use(f"b{i}", "Bash", {"command": "cat bigfile"}),
            ], ts=f"2026-08-01T00:0{i}:00Z"))
            records.append(fx.tool_result(f"r{i}", "x" * 50, ts=f"2026-08-01T00:0{i}:01Z"))
            records.append(fx.tool_result(f"b{i}", "y" * 100_000, ts=f"2026-08-01T00:0{i}:02Z"))
        fx.write(self.home, records, name="aaaa1111.jsonl")
        corp = corpus.load()
        rows = query.repeat_read_rows(corp, query.Filter())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reads"], 5)
        self.assertEqual(rows[0]["wasted_bytes"], 200)  # 4 re-reads x 50 bytes, not Bash's 100000

    def test_cache_ratio_reports_none_when_nothing_written(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        ratio = query.cache_ratio(corp, query.Filter())
        self.assertIsNone(ratio["ratio"])


class SubagentDispatchTest(CostQueryTestBase):
    def _write_parent_and_child(self, child_records, agent_type="general-purpose") -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Do the thing"})]),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, child_records, agent_id="childone01",
                          agent_type=agent_type)

    def test_sunk_flagged_for_non_complete_child(self) -> None:
        # Ends mid-tool: end_state is interrupted-tool, not complete.
        self._write_parent_and_child([
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "sleep 100"})]),
        ])
        corp = corpus.load()
        rows = query.subagent_dispatch_rows(corp, query.Filter())
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["sunk"])

    def test_complete_child_is_not_sunk(self) -> None:
        self._write_parent_and_child(fx.simple_session())
        corp = corpus.load()
        rows = query.subagent_dispatch_rows(corp, query.Filter())
        self.assertFalse(rows[0]["sunk"])

    def test_wasted_requires_sunk_and_cost_above_threshold(self) -> None:
        self._write_parent_and_child([
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "sleep 100"})],
                        usage={"input_tokens": 1_000_000, "output_tokens": 0},
                        model="claude-opus-5"),
        ])
        corp = corpus.load()
        rows = query.subagent_dispatch_rows(corp, query.Filter())
        self.assertTrue(rows[0]["sunk"])
        self.assertGreater(rows[0]["cost_usd"], query.BLOAT_DISPATCH_USD)
        self.assertTrue(rows[0]["wasted"])

    def test_summary_states_sample_size(self) -> None:
        self._write_parent_and_child(fx.simple_session())
        corp = corpus.load()
        summary = query.subagent_cost_summary(corp, query.Filter())
        self.assertEqual(summary["dispatches"], 1)
        self.assertEqual(summary["resolved"], 1)
        self.assertIn("parent_cost_total", summary)
        self.assertIn("child_cost_total", summary)


class ClaudeJsonCostCheckTest(CostQueryTestBase):
    def test_matches_when_this_session_is_the_project_last_session(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        (self.home.parent / ".claude.json").write_text(
            __import__("json").dumps({"projects": {fx.CWD: {
                "lastSessionId": fx.SID,
                "lastModelUsage": {"claude-opus-5": {"costUSD": 0.5}},
            }}}), encoding="utf-8")
        corp = corpus.load()
        entry = corp.sessions[0]
        check = query.claude_json_cost_check(corp, entry)
        self.assertIsNotNone(check)
        self.assertAlmostEqual(check["claude_json_cost"], 0.5)
        self.assertAlmostEqual(check["sk_cost"], entry.session.cost_usd)

    def test_none_when_session_is_not_the_last_one(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        (self.home.parent / ".claude.json").write_text(
            __import__("json").dumps({"projects": {fx.CWD: {
                "lastSessionId": "someone-else",
                "lastModelUsage": {"claude-opus-5": {"costUSD": 0.5}},
            }}}), encoding="utf-8")
        corp = corpus.load()
        entry = corp.sessions[0]
        self.assertIsNone(query.claude_json_cost_check(corp, entry))

    def test_none_when_no_claude_json_present(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        entry = corp.sessions[0]
        self.assertIsNone(query.claude_json_cost_check(corp, entry))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
