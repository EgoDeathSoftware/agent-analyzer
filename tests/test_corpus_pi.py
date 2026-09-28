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

    def test_session_ending_on_api_error_is_not_complete(self) -> None:
        fx.write(self.pi_home, fx.api_error_session())
        session = corpus.load().sessions[0].session
        self.assertEqual(session.end_state, "error-cascade")
        self.assertIn("Refresh token expired", session.end_reason)

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

    def test_subagent_dispatch_resolves_through_children_rows(self) -> None:
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
        parent = query.find_session(loaded, fx.SID)
        rows = query.children_rows(parent, loaded)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["resolved"])
        self.assertEqual(rows[0]["agent_type"], "scout")
        self.assertEqual(rows[0]["state"], "complete")


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
