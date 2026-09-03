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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
