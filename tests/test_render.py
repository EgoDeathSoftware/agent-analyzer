"""Tests for budget enforcement in report rendering.

The load-bearing property is that truncation is never silent: a capped report must say what it
dropped, or a caller reads a partial answer as a complete one.
"""

from __future__ import annotations

import json
import unittest

from sessionkit.render import OMIT_HINT, Report, human_cost


class BudgetTest(unittest.TestCase):
    """Tables shrink to fit and announce what was dropped."""

    ROWS = [[f"session-{i:03d}", f"value-{i}", str(i)] for i in range(400)]

    def test_truncation_is_announced(self) -> None:
        out = Report(budget_kb=1.0).table(["a", "b", "c"], self.ROWS).render()
        self.assertIn("more row(s) omitted", out)
        self.assertIn(OMIT_HINT, out)

    def test_budget_is_respected(self) -> None:
        out = Report(budget_kb=1.0).table(["a", "b", "c"], self.ROWS).render()
        self.assertLess(len(out), 1024 * 1.4, "output should stay near its budget")

    def test_small_table_is_not_truncated(self) -> None:
        out = Report(budget_kb=8.0).table(["a"], [["one"], ["two"]]).render()
        self.assertNotIn("omitted", out)
        self.assertIn("one", out)
        self.assertIn("two", out)

    def test_empty_table_renders_placeholder(self) -> None:
        self.assertIn("(none)", Report().table(["a"], []).render())

    def test_json_mode_reports_omitted_count(self) -> None:
        out = Report(as_json=True, budget_kb=1.0).table(["a", "b", "c"], self.ROWS).render()
        doc = json.loads(out)
        self.assertIn("omitted", doc)
        self.assertGreater(doc["omitted"]["rows"], 0)
        self.assertEqual(len(doc["rows"]) + doc["omitted"]["rows"], len(self.ROWS))

    def test_json_mode_shapes_rows_as_objects(self) -> None:
        out = Report(as_json=True).table(["x", "y"], [["1", "2"]]).render()
        self.assertEqual(json.loads(out)["rows"][0], {"x": "1", "y": "2"})

    def test_meta_and_sections_render(self) -> None:
        out = Report().meta(count=3).section("Title").text("body").render()
        self.assertIn("count=3", out)
        self.assertIn("## Title", out)
        self.assertIn("body", out)

    def test_cells_are_single_line(self) -> None:
        out = Report().table(["a"], [["multi\nline\tcell"]]).render()
        self.assertIn("multi line cell", out)

    def test_none_cell_renders_dash(self) -> None:
        self.assertIn("-", Report().table(["a"], [[None]]).render())

    def test_long_cell_is_truncated_with_marker_in_text_mode(self) -> None:
        long = "x" * 300
        out = Report(budget_kb=8.0).table(["a"], [[long]]).render()
        self.assertNotIn(long, out)
        self.assertIn("…", out)

    def test_full_flag_disables_cell_truncation(self) -> None:
        long = "x" * 300
        out = Report(budget_kb=8.0, full=True).table(["a"], [[long]]).render()
        self.assertIn(long, out)

    def test_json_mode_never_truncates_cells_regardless_of_full(self) -> None:
        long = "x" * 300
        out = Report(as_json=True, budget_kb=8.0).table(["a"], [[long]]).render()
        self.assertEqual(json.loads(out)["rows"][0]["a"], long)


class CostFormatTest(unittest.TestCase):
    """human_cost keeps small amounts legible."""

    def test_large_amount(self) -> None:
        self.assertEqual(human_cost(1234.5), "$1,234.50")

    def test_sub_cent_amount_keeps_precision(self) -> None:
        self.assertEqual(human_cost(0.0012), "$0.0012")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
