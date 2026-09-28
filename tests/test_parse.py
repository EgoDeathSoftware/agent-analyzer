"""Tests for transcript parsing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sessionkit.parse import basename_of, digest, parse_file, project_key, read_line
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

    def test_file_operations_carry_their_line_number(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/a.py"})]),
        ])
        self.assertEqual(session.files[0].line, 2)  # the assistant record is JSONL line 2

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

    def test_progress_records_become_system_events(self) -> None:
        path = fx.write_subagent(self.tmp, [
            fx.user("go"),
            {"type": "progress", "sessionId": fx.SID, "timestamp": "2026-08-01T00:00:05Z",
             "data": {"type": "hook_progress", "hookName": "PreToolUse:Bash"}},
        ])
        session = parse_file(path, "host")
        self.assertIn("hook_progress", [e.subtype for e in session.sysev])

    def test_result_line_is_recorded_on_the_tool_call(self) -> None:
        session = self.parse([
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "ls"})]),
            fx.tool_result("t1", "ok"),
        ])
        self.assertEqual(session.tools[0].result_line, 3)


class ReadLineTest(unittest.TestCase):
    def test_reads_exact_line_and_none_past_the_end(self) -> None:
        path = fx.write(Path(tempfile.mkdtemp()), fx.simple_session(), name="aaaa1111.jsonl")
        rec = read_line(str(path), 1)
        self.assertEqual(rec["type"], "user")
        self.assertIsNone(read_line(str(path), 999))


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

    def test_msg_id_is_the_api_message_id(self) -> None:
        records = [fx.user("go")]
        for ts, block in (("2026-08-01T00:00:01Z", {"type": "thinking", "thinking": "hm"}),
                          ("2026-08-01T00:00:02Z", {"type": "text", "text": "ok"})):
            record = fx.assistant([block], ts=ts, stop_reason="end_turn")
            record["message"]["id"] = "msg_01"
            records.append(record)
        session = self.parse(records)
        self.assertEqual([m.msg_id for m in session.messages], ["", "msg_01", "msg_01"])

    def test_is_prompt_marks_only_real_user_prompts(self) -> None:
        texts = ["fix the build", "<local-command-caveat>Caveat: generated locally.",
                 "<task-notification><task-id>t1</task-id>", "<command-name>/clear</command-name>",
                 "<local-command-stdout>ok</local-command-stdout>",
                 "<system-reminder>note</system-reminder>"]
        records = [fx.user(text, ts=f"2026-08-01T00:00:0{i}Z") for i, text in enumerate(texts)]
        records.append(fx.user("Continue from where you left off.", ts="2026-08-01T00:00:08Z",
                               isMeta=True))
        session = self.parse(records)
        self.assertEqual([m.is_prompt for m in session.messages],
                         [True, False, False, False, False, False, False])

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
