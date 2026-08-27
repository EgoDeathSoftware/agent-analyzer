"""Tests for transcript parsing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sessionkit.parse import basename_of, digest, parse_file, project_key
from tests import fixtures as fx


class ParseTest(unittest.TestCase):
    """Parsing a transcript into the normalised event stream."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parse(self, records: list[dict], **kw) -> "object":
        """Write and parse a transcript."""
        return parse_file(fx.write(self.tmp, records, **kw), "host")

    def test_pairs_tool_use_with_result(self) -> None:
        session = self.parse(fx.simple_session())
        self.assertEqual(len(session.tools), 1)
        call = session.tools[0]
        self.assertEqual(call.name, "Read")
        self.assertFalse(call.is_error)
        self.assertEqual(call.err_class, "")
        self.assertIn("file contents", call.output_preview)

    def test_unresolved_tool_use_is_flagged(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([fx.tool_use("t9", "Bash", {"command": "sleep 1"})]),
        ])
        self.assertEqual(session.tools[0].err_class, "no-result")

    def test_error_result_marked(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "grep x"})]),
            fx.tool_result("t1", "BLOCKED: Use rg instead of grep", is_error=True),
        ])
        self.assertTrue(session.tools[0].is_error)

    def test_counts_only_real_user_turns(self) -> None:
        session = self.parse([
            fx.user("first"),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/a"})]),
            fx.tool_result("t1", "ok"),          # tool transport, not a turn
            fx.user("meta note", isMeta=True),   # meta, not a turn
            fx.user("second"),
        ])
        self.assertEqual(session.turns, 2)
        self.assertEqual(session.first_prompt, "first")

    def test_records_file_operations(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([
                fx.tool_use("t1", "Read", {"file_path": "/a.py"}),
                fx.tool_use("t2", "Edit", {"file_path": "/b.py"}),
                fx.tool_use("t3", "Bash", {"command": "ls"}),
            ]),
        ])
        self.assertEqual([(f.path, f.op) for f in session.files],
                         [("/a.py", "read"), ("/b.py", "edit")])

    def test_accumulates_usage_and_cost(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([{"type": "text", "text": "hi"}], model="claude-opus-5",
                         usage={"input_tokens": 1000, "output_tokens": 2000,
                                "cache_read_input_tokens": 500,
                                "cache_creation_input_tokens": 100}),
        ])
        self.assertEqual(session.tok_in, 1000)
        self.assertEqual(session.tok_out, 2000)
        self.assertEqual(session.tok_cache_read, 500)
        # per million: input $5, output $25, cache read $0.50, cache write $6.25
        expected = 1000 * 5e-6 + 2000 * 25e-6 + 500 * 5e-7 + 100 * 6.25e-6
        self.assertAlmostEqual(session.cost_usd, expected, places=9)

    def test_tolerates_malformed_lines(self) -> None:
        path = fx.write(self.tmp, fx.simple_session())
        path.write_text(path.read_text() + "{not json\n", encoding="utf-8")
        session = parse_file(path, "host")
        self.assertEqual(session.malformed_lines, 1)
        self.assertEqual(len(session.tools), 1, "valid records still parse")

    def test_string_and_block_tool_result_bodies(self) -> None:
        """``tool_result.content`` is a string in some records and a block list in others."""
        records = [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "ls"})]),
            {"type": "user", "sessionId": fx.SID, "timestamp": "2026-08-01T00:00:02Z",
             "uuid": "r1", "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1",
                  "content": [{"type": "text", "text": "block form"}]}]}},
        ]
        session = self.parse(records)
        self.assertIn("block form", session.tools[0].output_preview)

    def test_subagent_keyed_by_agent_id_not_session_id(self) -> None:
        """The parent's sessionId must not become the subagent's identity."""
        path = fx.write_subagent(self.tmp, fx.simple_session(), agent_id="deadbeef")
        session = parse_file(path, "host")
        self.assertEqual(session.sid, "deadbeef")
        self.assertEqual(session.parent_sid, fx.SID)
        self.assertTrue(session.is_subagent)
        self.assertEqual(session.agent_type, "Explore")

    def test_progress_records_become_system_events(self) -> None:
        path = fx.write_subagent(self.tmp, [
            fx.user("go"),
            {"type": "progress", "sessionId": fx.SID, "timestamp": "2026-08-01T00:00:05Z",
             "data": {"type": "hook_progress", "hookName": "PreToolUse:Bash"}},
        ])
        session = parse_file(path, "host")
        self.assertIn("hook_progress", [e.subtype for e in session.sysev])


class KeyTest(unittest.TestCase):
    """Project identity and digest helpers."""

    def test_basename_handles_both_separators(self) -> None:
        self.assertEqual(basename_of("/home/dev/Proj"), "Proj")
        self.assertEqual(basename_of(r"C:\Users\dev\Proj"), "Proj")
        self.assertEqual(basename_of("/home/dev/Proj/"), "Proj")
        self.assertEqual(basename_of(""), "")

    def test_project_key_merges_across_platforms(self) -> None:
        self.assertEqual(project_key("/home/dev/MyApp", "wsl", "d"),
                         project_key(r"C:\Users\dev\myapp", "windows", "d"))

    def test_project_key_falls_back_without_cwd(self) -> None:
        self.assertEqual(project_key("", "host", "-some-dir"), "host:-some-dir")

    def test_digest_is_stable_and_discriminating(self) -> None:
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))
        self.assertNotEqual(digest({"a": 1}), digest({"a": 2}))

    def test_digest_survives_unserialisable_input(self) -> None:
        self.assertTrue(digest({"x": object()}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
