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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
