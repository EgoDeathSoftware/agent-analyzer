"""Full-corpus text search: raw-line scanning plus the tool-results/ spill fallback.

`compile_query` and `scan_corpus`/`search_rows` are exercised directly (no CLI) in
`SearchScan`; `SearchCli` drives the whole stack through `cli.main`, matching the split used
by `test_tail_signal.py`/`test_tail.py`.
"""

from __future__ import annotations

import unittest

from sessionkit import search


class SearchScan(unittest.TestCase):
    def test_plain_query_matches_case_insensitively(self) -> None:
        pattern = search.compile_query("Banana", regex=False, case_sensitive=False)
        self.assertIsNotNone(pattern.search("i ate a banana today"))

    def test_case_sensitive_flag_narrows_the_match(self) -> None:
        pattern = search.compile_query("Banana", regex=False, case_sensitive=True)
        self.assertIsNone(pattern.search("i ate a banana today"))
        self.assertIsNotNone(pattern.search("i ate a Banana today"))

    def test_plain_query_escapes_regex_metacharacters(self) -> None:
        pattern = search.compile_query("a.b(c)", regex=False, case_sensitive=True)
        self.assertIsNone(pattern.search("axbycz"))  # would match if '.' were a wildcard
        self.assertIsNotNone(pattern.search("x a.b(c) y"))

    def test_regex_flag_compiles_a_real_pattern(self) -> None:
        pattern = search.compile_query(r"err(or|ed)", regex=True, case_sensitive=False)
        self.assertIsNotNone(pattern.search("it Errored badly"))

    def test_bad_regex_pattern_exits(self) -> None:
        with self.assertRaises(SystemExit):
            search.compile_query("(unclosed", regex=True, case_sensitive=False)

    def test_empty_query_exits(self) -> None:
        with self.assertRaises(SystemExit):
            search.compile_query("", regex=False, case_sensitive=False)

    def _source(self, root):
        from sessionkit.sources import Source
        return Source(id="host", kind="claude-code", layout="single", location="host",
                     root=root, reachable=True)

    def test_scan_corpus_finds_hit_in_a_plain_message(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [fx.user("please find the loose banana crate")])
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            hits = search.scan_corpus([self._source(home)], pattern)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "jsonl")
        self.assertIn("banana", hits[0].raw_line.lower())

    def test_scan_corpus_finds_hit_only_in_toolUseResult_not_the_stub(self) -> None:
        # The transcript's message.content stub never contains the needle — only the sibling
        # toolUseResult field does, exactly reproducing PLAN.md §3.2.1's two-copies problem.
        # ToolCall.output_preview (parse.py) is built from message.content alone, so a scan of
        # any in-memory preview would report zero hits here; the raw line must not.
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [
                fx.user("run it"),
                fx.assistant([fx.tool_use("t1", "Bash", {"command": "grep -R banana"})]),
                fx.tool_result("t1", "Output too large (93.7KB) — see <persisted-output>",
                               toolUseResult={"content": "x" * 3000 + " needle-in-haystack "
                                              + "y" * 3000}),
            ])
            pattern = search.compile_query("needle-in-haystack", regex=False,
                                           case_sensitive=False)
            hits = search.scan_corpus([self._source(home)], pattern)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("needle-in-haystack",
                         "Output too large (93.7KB) — see <persisted-output>")  # sanity

    def test_scan_corpus_max_hits_per_file_caps_results(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [fx.user(f"banana #{i}", ts=f"2026-08-01T00:0{i}:00Z")
                            for i in range(5)])
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            hits = search.scan_corpus([self._source(home)], pattern, max_hits_per_file=2)
        self.assertEqual(len(hits), 2)

    def test_scan_corpus_skips_unreachable_sources(self) -> None:
        from sessionkit.sources import Source
        from pathlib import Path
        unreachable = Source(id="gone", kind="claude-code", layout="single", location="host",
                             root=Path("/does/not/exist"), reachable=False)
        pattern = search.compile_query("banana", regex=False, case_sensitive=False)
        self.assertEqual(search.scan_corpus([unreachable], pattern), [])

    def _write_spill(self, home, project_dirname, sid, tool_use_id, text):
        from pathlib import Path
        spill_dir = home / "projects" / project_dirname / sid / "tool-results"
        spill_dir.mkdir(parents=True, exist_ok=True)
        (spill_dir / f"{tool_use_id}.txt").write_text(text, encoding="utf-8")

    def test_scan_corpus_finds_hit_in_a_spill_file_not_in_the_transcript(self) -> None:
        # The transcript itself never mentions the needle: this is the "aged out" case
        # (PLAN.md §3.2.1) where only the on-disk spill copy still carries the full text.
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [
                fx.user("run it"),
                fx.assistant([fx.tool_use("toolu_spill1", "Bash", {"command": "true"})]),
                fx.tool_result("toolu_spill1", "Output too large (9KB)"),
            ])
            self._write_spill(home, "-home-dev-myproject", fx.SID, "toolu_spill1",
                              "…" * 10 + " needle-in-spill " + "…" * 10)
            pattern = search.compile_query("needle-in-spill", regex=False,
                                           case_sensitive=False)
            hits = search.scan_corpus([self._source(home)], pattern)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "spill")
        self.assertTrue(hits[0].path.endswith("toolu_spill1.txt"))

    def test_scan_corpus_tolerates_a_source_with_no_projects_dir(self) -> None:
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            self.assertEqual(search.scan_corpus([self._source(Path(tmp))], pattern), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
