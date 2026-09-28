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
