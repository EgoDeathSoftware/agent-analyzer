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

    def test_a_sibling_call_with_empty_ts_produces_no_window(self) -> None:
        child = df.session(sid="child", tools=[df.call(
            55, "bash", {"command": "bash bench.sh"}, at="",
            result_at=df.ts(101), dur_ms=41 * 60_000)])
        found = resource_interference([("proj", _parent(65)), ("proj", child)])
        self.assertEqual(found[0], [])

    def test_a_sibling_call_with_empty_result_ts_produces_no_window(self) -> None:
        child = df.session(sid="child", tools=[df.call(
            55, "bash", {"command": "bash bench.sh"}, at=df.ts(60),
            result_at="", dur_ms=41 * 60_000)])
        found = resource_interference([("proj", _parent(65)), ("proj", child)])
        self.assertEqual(found[0], [])

    def test_mixed_z_and_offset_timestamps_still_overlap(self) -> None:
        """The error's timestamp uses ``Z`` and the window's boundaries use ``+00:00`` (or
        vice versa) — both are the same instant and must still compare as overlapping."""
        parent = df.session(sid="parent", messages=[
            df.msg(1, "user", text="go", at=df.ts(0)),
            df.msg(79, stop="error", error_text="Request timed out.",
                  at=df.ts(65).replace("Z", "+00:00"))])
        child = df.session(sid="child", tools=[df.call(
            55, "bash", {"command": "bash bench.sh"}, at=df.ts(60).replace("Z", "+00:00"),
            result_at=df.ts(101), dur_ms=41 * 60_000)])
        found = resource_interference([("proj", parent), ("proj", child)])
        self.assertEqual([a.lines for a in found[0]], [[79]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
