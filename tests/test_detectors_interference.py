"""Model errors that coincide with another session's long-running command."""

from __future__ import annotations

import unittest

from sessionkit.detectors.interference import resource_interference
from tests import detector_fixtures as df


def _parent(error_at: float):
    return df.session(sid="parent", messages=[
        df.msg(1, "user", text="go", at=df.ts(0)),
        df.msg(79, stop="error", error_text="Request timed out.", at=df.ts(error_at))])


def _child(start: float, minutes: float):
    return df.session(sid="child", tools=[df.call(
        55, "bash", {"command": "bash bench.sh"}, at=df.ts(start),
        result_at=df.ts(start + minutes), dur_ms=int(minutes * 60_000))])


class InterferenceTest(unittest.TestCase):
    def test_error_inside_a_sibling_command_window(self) -> None:
        found = resource_interference([("proj", _parent(65)), ("proj", _child(60, 41))])
        self.assertEqual([a.lines for a in found[0]], [[79]])
        self.assertEqual(found[1], [])

    def test_other_projects_do_not_interfere(self) -> None:
        found = resource_interference([("proj", _parent(65)), ("other", _child(60, 41))])
        self.assertEqual(found[0], [])

    def test_short_commands_are_ignored(self) -> None:
        found = resource_interference([("proj", _parent(60.5)), ("proj", _child(60, 1))])
        self.assertEqual(found[0], [])

    def test_errors_without_timestamps_are_skipped(self) -> None:
        parent = df.session(sid="parent", messages=[df.msg(79, stop="error",
                                                           error_text="Request timed out.")])
        self.assertEqual(resource_interference([("proj", parent), ("proj", _child(60, 41))])[0],
                         [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
