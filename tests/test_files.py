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


class UncommittedIntersection(FilesSessionScoped):
    def _init_git_repo(self, root: Path) -> None:
        import subprocess
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
        (root / "README.md").write_text("hello\n")
        (root / "other.md").write_text("hi\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True,
                       env=env)

    def _session_touching(self, root: Path, files: list[str]) -> None:
        # Rewrite CWD in fixture calls by mutating the module attribute for this test.
        orig_cwd = fx.CWD
        fx.CWD = str(root)
        try:
            records = [fx.user("edit")]
            for i, path in enumerate(files, start=1):
                records.append(
                    fx.assistant([fx.tool_use(f"t{i}", "Edit",
                                              {"file_path": path, "old_string": "x",
                                               "new_string": "y"})],
                                 ts=f"2026-08-01T00:00:0{i}Z"))
                records.append(fx.tool_result(f"t{i}", "ok",
                                              ts=f"2026-08-01T00:00:1{i}Z"))
            fx.write(self.home, records, name=f"{fx.SID}.jsonl")
        finally:
            fx.CWD = orig_cwd

    def test_dirty_file_appears_when_session_touched_it(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        self._init_git_repo(repo)
        (repo / "README.md").write_text("modified\n")  # dirty
        self._session_touching(repo, [str(repo / "README.md"), str(repo / "other.md")])
        out = self._run("files", fx.SID, "--uncommitted", "--json")
        payload = json.loads(out)
        paths = {r["path"] for r in payload["files"]}
        self.assertIn(str(repo / "README.md"), paths)
        self.assertNotIn(str(repo / "other.md"), paths)

    def test_ignored_file_never_appears(self) -> None:
        repo = self.tmp / "repo2"
        repo.mkdir()
        self._init_git_repo(repo)
        (repo / ".gitignore").write_text("secrets.env\n")
        (repo / "secrets.env").write_text("token=abc\n")
        self._session_touching(repo, [str(repo / "secrets.env")])
        out = self._run("files", fx.SID, "--uncommitted", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["files"], [],
                         "gitignored files are not listed by git status; the intersection "
                         "correctly drops them")

    def test_not_a_repo_notes_the_reason(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self._session_touching(plain, [str(plain / "a.py")])
        out = self._run("files", fx.SID, "--uncommitted")
        self.assertIn("not a git repo", out)

    def test_missing_git_binary_notes_the_reason(self) -> None:
        repo = self.tmp / "repo3"
        repo.mkdir()
        self._init_git_repo(repo)
        self._session_touching(repo, [str(repo / "README.md")])
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            out = self._run("files", fx.SID, "--uncommitted")
        self.assertIn("git binary not found", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
