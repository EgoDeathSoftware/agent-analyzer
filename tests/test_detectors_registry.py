"""The detector registry and the taxonomy agree, and empty sessions are handled."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from sessionkit import detectors, taxonomy
from sessionkit.classify import derive_end_state
from sessionkit.detectors import detect
from tests import detector_fixtures as df

_SOURCE = "".join(p.read_text(encoding="utf-8")
                  for p in Path(detectors.__file__).parent.glob("*.py"))
_LITERAL_KINDS = set(re.findall(r'Anomaly\(\s*"([a-z-]+)"', _SOURCE))


class RegistryTest(unittest.TestCase):
    def test_empty_sessions(self) -> None:
        for harness in ("pi", "claude-code", ""):
            with self.subTest(harness=harness):
                session = df.session(harness)
                self.assertEqual(detect(session), [])
                self.assertEqual(derive_end_state(session)[0], "unknown")

    def test_every_emitted_kind_is_in_the_taxonomy(self) -> None:
        self.assertTrue(_LITERAL_KINDS)
        self.assertEqual({k for k in _LITERAL_KINDS if taxonomy.lookup(k) is None}, set())

    def test_every_structural_or_candidate_kind_has_a_detector(self) -> None:
        kinds = {c.name for c in taxonomy.FINDING_KINDS if c.tier != taxonomy.TIER_J}
        self.assertEqual(kinds - _LITERAL_KINDS, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
