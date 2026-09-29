"""Lifecycle detectors: stalls, hangs, truncated transcripts, user interrupts."""

from __future__ import annotations

import unittest

from sessionkit.detectors import detect, lifecycle
from sessionkit.taxonomy import THRESHOLDS
from tests import detector_fixtures as df


def _kinds(session, live: bool = False) -> list[str]:
    return [a.kind for a in detect(session, live)]


class StallTest(unittest.TestCase):
    CASES = [
        ("pi", "read", {}, 200_000, False),
        ("pi", "read", {}, 420_000, True),
        ("pi", "bash", {"command": "x"}, 700_000, False),
        ("pi", "bash", {"command": "x"}, 1_300_000, True),
        ("pi", "bash", {"command": "x", "timeout": 30}, 28_000, True),
        ("pi", "bash", {"command": "x", "timeout": 30}, 10_000, False),
        ("claude-code", "Bash", {"command": "x", "timeout": 600_000}, 400_000, False),
        ("pi", "edit", {"path": "/a"}, 900_000, False),
        ("claude-code", "AskUserQuestion", {}, 900_000, False),
        ("pi", "subagent", {}, 3_600_000, False),
    ]
    MIN = 60_000
    APPROVAL_CASES = [
        ("claude-code", "Read", {}, 7 * MIN, False),
        ("claude-code", "Grep", {}, 7 * MIN, False),
        ("claude-code", "Bash", {"command": "x", "timeout": 60_000}, 20 * MIN, False),
        ("claude-code", "Bash", {"command": "x", "timeout": 600_000}, 580_000, True),
        ("claude-code", "Bash", {"command": "x", "timeout": 600_000}, 620_000, True),
        ("claude-code", "Bash", {"command": "x", "timeout": 600_000}, 11 * MIN, False),
        ("pi", "read", {}, 7 * MIN, True),
        ("claude-code", "Bash", {"command": "x"}, 15 * MIN, False),
        ("claude-code", "Bash", {"command": "x"}, 25 * MIN, True),
        ("claude-code", "WebFetch", {}, 6 * MIN, True),
        ("claude-code", "WebFetch", {}, 4 * MIN, False),
        ("claude-code", "WebSearch", {}, 6 * MIN, True),
        ("claude-code", "mcp__srv__lookup", {}, 6 * MIN, True),
    ]

    def test_cases(self) -> None:
        for harness, name, args, dur, expected in self.CASES:
            with self.subTest(harness=harness, name=name, dur=dur):
                s = df.session(harness, tools=[df.call(1, name, args, dur_ms=dur)])
                self.assertEqual("stall" in _kinds(s), expected)

    def test_claude_code_approval_wait_is_not_a_stall(self) -> None:
        for harness, name, args, dur, expected in self.APPROVAL_CASES:
            with self.subTest(harness=harness, name=name, dur=dur):
                s = df.session(harness, tools=[df.call(1, name, args, dur_ms=dur)])
                self.assertEqual("stall" in _kinds(s), expected)

    def test_thresholds(self) -> None:
        self.assertEqual(THRESHOLDS["stall_ms"], 300_000)
        self.assertEqual(THRESHOLDS["stall_exec_ms"], 1_200_000)


class HungTest(unittest.TestCase):
    def test_ended_waiting_on_a_command(self) -> None:
        s = df.session(tools=[df.call(3, err_class="no-result")], end_state="interrupted-tool")
        self.assertIn("hung", _kinds(s))

    def test_waiting_on_the_user_is_not_hung(self) -> None:
        s = df.session("claude-code", tools=[df.call(3, "AskUserQuestion", err_class="no-result")],
                       end_state="interrupted-tool")
        self.assertNotIn("hung", _kinds(s))

    def test_a_live_session_is_never_hung(self) -> None:
        s = df.session(tools=[df.call(3, err_class="no-result")], end_state="interrupted-tool")
        self.assertNotIn("hung", _kinds(s, live=True))

    def test_pi_turn_that_never_finished_streaming(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go"), df.msg(2, stop="")])
        self.assertIn("hung", _kinds(s))

    def test_claude_code_null_stop_records_are_not_hung(self) -> None:
        s = df.session("claude-code", messages=[df.msg(1, "user", text="go"),
                                                df.msg(2, text="ok", stop="")])
        self.assertNotIn("hung", _kinds(s))


class AbruptTruncationTest(unittest.TestCase):
    def test_partial_last_line(self) -> None:
        self.assertIn("abrupt-truncation", _kinds(df.session(last_line_malformed=True)))
        self.assertNotIn("abrupt-truncation",
                         _kinds(df.session(last_line_malformed=True), live=True))


class UserInterruptTest(unittest.TestCase):
    def test_pi_abort_then_redirect(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="set up profiles"),
                                 df.msg(33, stop="aborted"),
                                 df.msg(34, "user", text="just write a script")])
        found = lifecycle.user_interrupt(s)
        self.assertEqual([a.lines for a in found], [[33, 34]])

    def test_claude_code_marker_then_redirect(self) -> None:
        s = df.session("claude-code", messages=[
            df.msg(1, "user", text="go"),
            df.msg(5, "user", text="[Request interrupted by user]"),
            df.msg(6, "user", text="no, do it this way")])
        self.assertEqual([a.lines for a in lifecycle.user_interrupt(s)], [[5, 6]])

    def test_non_prompt_user_records_are_not_the_redirect(self) -> None:
        s = df.session("claude-code", messages=[
            df.msg(1, "user", text="go"),
            df.msg(5, "user", text="[Request interrupted by user]"),
            df.msg(6, "user", text="<local-command-caveat>Caveat: ...", is_prompt=False),
            df.msg(7, "user", text="no, do it this way")])
        self.assertEqual([a.lines for a in lifecycle.user_interrupt(s)], [[5, 7]])

    def test_one_redirect_is_reported_once(self) -> None:
        s = df.session(tools=[df.call(32, is_error=True, err_class="aborted")],
                       messages=[df.msg(33, stop="aborted"), df.msg(34, "user", text="stop")])
        self.assertEqual(len(lifecycle.user_interrupt(s)), 1)

    def test_abort_without_a_redirect_is_not_an_interrupt_finding(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go"), df.msg(60, stop="aborted")])
        self.assertEqual(lifecycle.user_interrupt(s), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
