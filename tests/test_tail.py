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
        self.assertEqual(payload["tail_signal"], "completion_marker")
        self.assertEqual(payload["n"], 4)
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
        self.assertEqual(payload["tail_signal"], "completion_marker")


class TailCorpusScoped(TailSessionScoped):
    def _mid_tool_transcript(self, sid: str) -> None:
        # Assistant turn issues a Bash tool_use with no matching tool_result.
        # `sessionId` is fixed to `fx.SID` on every fixture record; the parser locks onto the
        # *first* record's sessionId, so it must be overridden here for the report's per-row
        # sid to reflect this transcript rather than colliding with every other fixture's.
        fx.write(self.home, [
            fx.user("run something", sessionId=sid),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "sleep 60"})]),
        ], name=f"{sid}.jsonl")

    def _complete_transcript(self, sid: str) -> None:
        fx.write(self.home, [
            fx.user("add a feature", sessionId=sid),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/home/dev/myproject/a.py"})]),
            fx.tool_result("t1", "file contents"),
            fx.assistant([{"type": "text", "text": "done"}], ts="2026-08-01T00:00:04Z"),
        ], name=f"{sid}.jsonl")

    def test_all_lists_non_complete_sessions_only(self) -> None:
        self._complete_transcript("aaaa1111")
        self._mid_tool_transcript("bbbb2222")  # interrupted-tool
        out = self._run("tail", "--all", "--json")
        payload = json.loads(out)
        sids = [row["sid"] for row in payload["candidates"]]
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
        self.assertEqual(row["tail_signal"], "mid_tool")

    def test_all_emits_omitted_count_when_budget_forces_drops(self) -> None:
        for sid_suffix in "abcdef":
            self._mid_tool_transcript(sid_suffix * 8)
        out = self._run("tail", "--all", "--budget-kb", "0.4")
        self.assertIn("more row(s) omitted", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
