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

    def _empty_scope(self):
        from sessionkit.query import Filter
        return Filter()

    def test_search_rows_returns_a_row_with_a_context_excerpt(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [
                fx.user("please find the loose banana crate"),
                fx.assistant([{"type": "text", "text": "found it, fixed."}],
                             ts="2026-08-01T00:00:01Z"),
            ])
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            rows, degraded = search.search_rows([self._source(home)], self._empty_scope(),
                                                 pattern)
        self.assertEqual(degraded, 0)
        self.assertEqual(len(rows), 1)
        self.assertIn("banana", rows[0]["excerpt"].lower())
        self.assertEqual(rows[0]["kind"], "msg")

    def test_search_rows_finds_a_hit_even_when_the_excerpt_cannot_quote_it(self) -> None:
        # The needle lives only in the top-level toolUseResult field (Task 2's key scenario).
        # Even query.full_output's re-read only ever looks inside message.content's
        # tool_result block, never at a sibling toolUseResult key, so it cannot recover this
        # text either — the row must still be returned. Recall over the session is what
        # matters; PLAN.md §7 Phase 4's acceptance is "returns the session that hit it", not a
        # guarantee about exemplar wording.
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
            rows, degraded = search.search_rows([self._source(home)], self._empty_scope(),
                                                 pattern)
        self.assertEqual(degraded, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sid"], fx.SID)

    def test_search_rows_marks_the_matched_line_in_the_excerpt(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [
                fx.user("start", ts="2026-08-01T00:00:00Z"),
                fx.assistant([{"type": "text", "text": "ok"}], ts="2026-08-01T00:00:01Z"),
                fx.user("find the banana", ts="2026-08-01T00:00:02Z"),
                fx.assistant([{"type": "text", "text": "found it"}],
                             ts="2026-08-01T00:00:03Z"),
            ])
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], self._empty_scope(), pattern,
                                         context=1)
        self.assertIn(">>", rows[0]["excerpt"])  # the hit line is marked, not just present

    def test_search_rows_respects_project_scope(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [fx.user("banana in project one")])
            other_dir = home / "projects" / "-home-dev-otherproject"
            other_dir.mkdir(parents=True)
            (other_dir / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
                '{"type":"user","sessionId":"22222222-2222-2222-2222-222222222222",'
                '"cwd":"/home/dev/otherproject","timestamp":"2026-08-01T00:00:00Z",'
                '"uuid":"u1","message":{"role":"user","content":"banana in project two"}}\n',
                encoding="utf-8")
            from sessionkit.query import Filter
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], Filter(project="otherproject"),
                                         pattern)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project"], "otherproject")

    def test_search_rows_per_session_caps_hits_across_the_whole_session(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [fx.user(f"banana #{i}", ts=f"2026-08-01T00:0{i}:00Z")
                            for i in range(4)])
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], self._empty_scope(), pattern,
                                         per_session=1)
        self.assertEqual(len(rows), 1)

    def test_search_rows_limit_caps_total_rows_across_sessions(self) -> None:
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for i in range(3):
                sid = f"{i}" * 8 + "-0000-0000-0000-000000000000"
                project = home / "projects" / "-home-dev-myproject"
                project.mkdir(parents=True, exist_ok=True)
                (project / f"{sid}.jsonl").write_text(
                    f'{{"type":"user","sessionId":"{sid}","cwd":"/home/dev/myproject",'
                    f'"timestamp":"2026-08-01T00:00:0{i}Z","uuid":"u{i}",'
                    f'"message":{{"role":"user","content":"banana session {i}"}}}}\n',
                    encoding="utf-8")
            pattern = search.compile_query("banana", regex=False, case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], self._empty_scope(), pattern,
                                         limit=2)
        self.assertEqual(len(rows), 2)

    def test_search_rows_resolves_a_spill_hit_to_its_tool_result_line(self) -> None:
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
                              "needle-in-spill")
            pattern = search.compile_query("needle-in-spill", regex=False,
                                           case_sensitive=False)
            rows, degraded = search.search_rows([self._source(home)], self._empty_scope(),
                                                 pattern)
        self.assertEqual(degraded, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sid"], fx.SID)
        self.assertEqual(rows[0]["kind"], "result")

    def test_search_rows_reports_degraded_when_spill_transcript_is_missing(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # A spill file with no sibling <sid>.jsonl at all — the "aged out" case where the
            # transcript itself is gone but the spill directory survived.
            self._write_spill(home, "-home-dev-myproject", "orphan-sid", "toolu_x",
                              "needle-in-spill")
            pattern = search.compile_query("needle-in-spill", regex=False,
                                           case_sensitive=False)
            rows, degraded = search.search_rows([self._source(home)], self._empty_scope(),
                                                 pattern)
        self.assertEqual(rows, [])
        self.assertEqual(degraded, 1)

    def test_search_rows_falls_back_to_the_raw_line_when_the_hit_line_has_no_entry(self) -> None:
        # `type: "mode"` is not one of the record types _Parser.feed() dispatches on
        # (sessionkit/parse.py) — it produces no Message/ToolCall/SysEvent, only updates to
        # cwd/timestamps via _meta(). Neighbouring lines *do* have entries, so `entries` is
        # non-empty even though nothing in it matches the hit line — the fallback must key off
        # `center`, not `entries`, or this would render unrelated neighbour text with no `>>`.
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [
                fx.user("start", ts="2026-08-01T00:00:00Z"),
                {"type": "mode", "sessionId": fx.SID, "cwd": fx.CWD,
                 "timestamp": "2026-08-01T00:00:01Z", "note": "needle-in-mode-record"},
                fx.assistant([{"type": "text", "text": "ok"}], ts="2026-08-01T00:00:02Z"),
            ])
            pattern = search.compile_query("needle-in-mode-record", regex=False,
                                           case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], self._empty_scope(), pattern)
        self.assertEqual(rows[0]["kind"], "raw")
        self.assertIn("needle-in-mode-record", rows[0]["excerpt"])

    def test_search_rows_deduplicates_a_hit_present_in_both_the_transcript_and_its_spill_copy(
        self,
    ) -> None:
        # PLAN.md §3.2.1: the transcript's toolUseResult/message.content copy and the on-disk
        # tool-results/*.txt spill copy normally coexist. A query matching both must resolve
        # to one row, not two. per_session=0 (unlimited) so the default per-session cap of 1
        # can't mask a real duplicate — both would-be rows share one sid.
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fx.write(home, [
                fx.user("run it"),
                fx.assistant([fx.tool_use("toolu_dup", "Bash", {"command": "true"})]),
                fx.tool_result("toolu_dup", "the needle-in-both-copies text"),
            ])
            self._write_spill(home, "-home-dev-myproject", fx.SID, "toolu_dup",
                              "the needle-in-both-copies text")
            pattern = search.compile_query("needle-in-both-copies", regex=False,
                                           case_sensitive=False)
            rows, degraded = search.search_rows([self._source(home)], self._empty_scope(),
                                                 pattern, per_session=0)
        self.assertEqual(degraded, 0)
        self.assertEqual(len(rows), 1)

    def test_search_rows_excerpt_shows_text_past_the_message_preview_cap(self) -> None:
        # MSG_PREVIEW is 200 chars (parse.py). If _context_entries ever reverted to using
        # Message.preview instead of query.full_message, this test would catch it.
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            long_text = ("x" * 250) + " needle-past-msg-cap"
            fx.write(home, [fx.user(long_text)])
            pattern = search.compile_query("needle-past-msg-cap", regex=False,
                                           case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], self._empty_scope(), pattern)
        self.assertIn("needle-past-msg-cap", rows[0]["excerpt"])

    def test_search_rows_excerpt_shows_tool_output_past_the_preview_cap(self) -> None:
        # OUTPUT_PREVIEW is 2000 chars (parse.py). Distinct from the toolUseResult case:
        # this text lives inside message.content's tool_result block, which query.full_output
        # *can* recover — if _context_entries ever reverted to ToolCall.output_preview, this
        # would catch it.
        import tempfile
        from pathlib import Path
        from tests import fixtures as fx
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            long_output = ("y" * 2200) + " needle-past-output-cap"
            fx.write(home, [
                fx.user("run it"),
                fx.assistant([fx.tool_use("t1", "Bash", {"command": "true"})]),
                fx.tool_result("t1", long_output),
            ])
            pattern = search.compile_query("needle-past-output-cap", regex=False,
                                           case_sensitive=False)
            rows, _ = search.search_rows([self._source(home)], self._empty_scope(), pattern)
        self.assertIn("needle-past-output-cap", rows[0]["excerpt"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
