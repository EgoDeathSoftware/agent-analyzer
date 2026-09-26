"""End-to-end tests: a synthetic pi corpus on disk, parsed through to reports."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionkit import corpus, query
from tests import fixtures_pi as fx


class PiCorpusTest(unittest.TestCase):
    """Parse a controlled pi corpus and query it, same shape as tests/test_corpus.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pi_home = self.tmp / "pi-agent"
        self.pi_home.mkdir()
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.tmp / "claude"),  # left absent: unreachable, on purpose
            "PI_DIR": str(self.pi_home),
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_finds_pi_session(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        loaded = corpus.load()
        self.assertEqual(len(loaded.sessions), 1)
        self.assertEqual(loaded.sessions[0].session.sid, fx.SID)
        self.assertEqual(loaded.sessions[0].session.source_id, "pi")
        self.assertEqual(loaded.failed, 0)

    def test_doctor_reports_pi_source(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        loaded = corpus.load()
        rows = {r["id"]: r for r in query.source_rows(loaded)}
        self.assertIn("pi", rows)
        self.assertTrue(rows["pi"]["reachable"])
        self.assertEqual(rows["pi"]["kind"], "pi")

    def test_source_filter_isolates_pi_sessions(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        loaded = corpus.load()
        f = query.Filter(source="pi")
        matched = [e for e in loaded.sessions if f.matches(e)]
        self.assertEqual(len(matched), 1)

    def test_load_session_finds_pi_session_by_id_prefix(self) -> None:
        fx.write(self.pi_home, fx.simple_session())
        entry = corpus.load_session(fx.SID[:8])
        self.assertIsNotNone(entry)
        self.assertEqual(entry.session.sid, fx.SID)

    def test_subagent_children_are_added_to_the_corpus(self) -> None:
        details = {"mode": "single", "results": [
            fx.subagent_result("scout", "find the bug", "found it in foo.py"),
        ]}
        fx.write(self.pi_home, [
            fx.header(),
            fx.user_msg("e1", None, "delegate this"),
            fx.assistant_msg("e2", "e1", [fx.tool_call_block(
                "c1", "subagent", {"agent": "scout", "task": "find the bug"})]),
            fx.tool_result_msg("e3", "e2", "c1", "subagent", "found it in foo.py",
                               details=details),
        ])
        loaded = corpus.load()
        self.assertEqual(len(loaded.sessions), 2, "parent + one synthetic subagent child")
        sids = {e.session.sid for e in loaded.sessions}
        self.assertIn(f"{fx.SID}:c1:0", sids)
