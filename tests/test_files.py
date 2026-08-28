"""End-to-end tests for `sk files`.

Session-scoped and project-scoped shapes; the `--uncommitted` git intersection is Task 5.
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


class FilesSessionScoped(unittest.TestCase):
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

    def _mixed_ops_transcript(self) -> None:
        fx.write(self.home, [
            fx.user("edit stuff"),
            fx.assistant([fx.tool_use("t1", "Read", {"file_path": "/home/dev/myproject/a.py"})]),
            fx.tool_result("t1", "content"),
            fx.assistant([fx.tool_use("t2", "Edit",
                                       {"file_path": "/home/dev/myproject/a.py",
                                        "old_string": "x", "new_string": "y"})],
                         ts="2026-08-01T00:00:03Z"),
            fx.tool_result("t2", "edited", ts="2026-08-01T00:00:04Z"),
            fx.assistant([fx.tool_use("t3", "Write",
                                       {"file_path": "/home/dev/myproject/b.py",
                                        "content": "print('hi')"})],
                         ts="2026-08-01T00:00:05Z"),
            fx.tool_result("t3", "wrote", ts="2026-08-01T00:00:06Z"),
        ], name=f"{fx.SID}.jsonl")

    def test_session_scoped_splits_op_counts(self) -> None:
        self._mixed_ops_transcript()
        out = self._run("files", fx.SID, "--json")
        payload = json.loads(out)
        rows = {r["path"]: r for r in payload["files"]}
        a = rows["/home/dev/myproject/a.py"]
        self.assertEqual([a["reads"], a["writes"], a["edits"]], ["1", "0", "1"])
        b = rows["/home/dev/myproject/b.py"]
        self.assertEqual([b["reads"], b["writes"], b["edits"]], ["0", "1", "0"])

    def test_missing_sid_exits_nonzero(self) -> None:
        self._mixed_ops_transcript()
        with self.assertRaises(SystemExit):
            self._run("files", "deadbeef")

    def test_project_rollup_aggregates_sessions(self) -> None:
        self._mixed_ops_transcript()
        # A second session in the same cwd touches a.py again.
        fx.write(self.home, [
            fx.user("more"),
            fx.assistant([fx.tool_use("t9", "Read",
                                       {"file_path": "/home/dev/myproject/a.py"})]),
            fx.tool_result("t9", "content"),
        ], name="ccccdddd.jsonl")
        out = self._run("files", "--json")  # corpus/project scope
        payload = json.loads(out)
        a_row = next(r for r in payload["files"] if r["path"] == "/home/dev/myproject/a.py")
        self.assertEqual(a_row["sessions"], "2", "two sessions touched a.py")

    def test_unknown_project_returns_empty_table_not_error(self) -> None:
        self._mixed_ops_transcript()
        out = self._run("files", "--project", "nonexistent", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["files"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
