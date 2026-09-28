"""Tests for the failure taxonomy table itself: names, tiers, families, hints."""

from __future__ import annotations

import unittest

from sessionkit import taxonomy

VALID_FAMILIES = {"generic", "tool-protocol", "generation", "loop", "lifecycle", "recovery",
                  "alignment", "environment"}
VALID_TIERS = {taxonomy.TIER_S, taxonomy.TIER_CANDIDATE, taxonomy.TIER_J}
VALID_TARGETS = {"model", "harness", "serving", "env", "none"}


class TaxonomyTableTest(unittest.TestCase):
    """The table is well formed: unique names, valid enums, a hint everywhere."""

    def test_names_are_unique_across_classes_and_kinds(self) -> None:
        names = [c.name for c in taxonomy.ALL_CLASSES]
        self.assertEqual(len(names), len(set(names)))

    def test_every_class_is_well_formed(self) -> None:
        for entry in taxonomy.ALL_CLASSES:
            with self.subTest(entry.name):
                self.assertIn(entry.family, VALID_FAMILIES)
                self.assertIn(entry.tier, VALID_TIERS)
                self.assertIn(entry.fix_target, VALID_TARGETS)
                self.assertTrue(entry.hint)

    def test_error_classes_carry_patterns(self) -> None:
        for entry in taxonomy.ERROR_CLASSES:
            with self.subTest(entry.name):
                self.assertTrue(entry.patterns)

    def test_lookup(self) -> None:
        self.assertEqual(taxonomy.lookup("repeat-tool").family, "loop")
        self.assertIsNone(taxonomy.lookup("no-such-kind"))

    def test_killed_is_registered_for_the_exit_code_mapping(self) -> None:
        self.assertEqual(taxonomy.classify_error("Exit code 137")[0], "killed")
        self.assertIsNotNone(taxonomy.lookup("killed"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
