"""Code and knowledge base stay in step: every taxonomy class is documented for the skill,
and every harness role is used by some detector."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from sessionkit import detectors, harness, taxonomy

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "skills" / "session-forensics" / "reference" / "failure-classes.md"


class ParityTest(unittest.TestCase):
    def test_every_class_has_exactly_one_reference_entry(self) -> None:
        headings = re.findall(r"^### `([a-z-]+)`", REFERENCE.read_text(encoding="utf-8"),
                              re.M)
        self.assertEqual(len(headings), len(set(headings)), "duplicate reference entry")
        names = {c.name for c in taxonomy.ALL_CLASSES}
        self.assertEqual(names - set(headings), set(), "classes missing from the reference")
        self.assertEqual(set(headings) - names, set(), "reference entries with no class")

    def test_every_role_is_used_by_a_detector(self) -> None:
        source = "".join(p.read_text(encoding="utf-8")
                         for p in Path(detectors.__file__).parent.glob("*.py"))
        for role in harness.ROLES:
            if role == "other":
                continue
            with self.subTest(role):
                self.assertIn(f'"{role}"', source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
