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
