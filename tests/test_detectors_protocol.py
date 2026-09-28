"""Tool-protocol and generation detectors."""

from __future__ import annotations

import unittest

from sessionkit.detectors import detect, generation, tool_protocol
from tests import detector_fixtures as df


def _kinds(session) -> list[str]:
    return [a.kind for a in detect(session)]


class ErrorClassFindingTest(unittest.TestCase):
    def test_surfaced_classes_become_findings(self) -> None:
        s = df.session(tools=[
            df.call(1, "edit", is_error=True, err_class="edit-mismatch"),
            df.call(2, is_error=True, err_class="exit-code"),
            df.call(3, is_error=True, err_class="aborted"),
        ], messages=[df.msg(4, stop="error", error_text="Request timed out.")])
        kinds = {a.kind: a for a in tool_protocol.error_classes(s)}
        self.assertEqual(set(kinds), {"edit-mismatch", "network"})
        self.assertEqual(kinds["network"].lines, [4])

    def test_clean_session_has_none(self) -> None:
        self.assertEqual(tool_protocol.error_classes(df.session(tools=[df.call(1)])), [])


class ToolTextLeakTest(unittest.TestCase):
    def test_leak_without_a_real_call(self) -> None:
        s = df.session(messages=[df.msg(2, markup_leak="json-call", stop="end")])
        self.assertIn("tool-text-leak", _kinds(s))

    def test_markup_alongside_a_real_call_is_not_a_leak(self) -> None:
        s = df.session(messages=[df.msg(2, markup_leak="json-call", tool_calls=1)])
        self.assertNotIn("tool-text-leak", _kinds(s))


class GenerationTest(unittest.TestCase):
    def test_empty_turn(self) -> None:
        s = df.session(messages=[df.msg(2, stop="end", reasoning_len=500)])
        self.assertIn("empty-turn", _kinds(s))

    def test_claude_code_intermediate_records_are_not_empty_turns(self) -> None:
        s = df.session("claude-code", messages=[df.msg(2, stop="", reasoning_len=300),
                                                df.msg(3, stop="tool_use", tool_calls=1)])
        self.assertNotIn("empty-turn", _kinds(s))

    def test_degeneration_includes_aborted_turns(self) -> None:
        s = df.session(messages=[df.msg(60, stop="aborted", repeat_ratio=0.85)])
        self.assertIn("degeneration", _kinds(s))

    def test_varied_text_is_not_degeneration(self) -> None:
        s = df.session(messages=[df.msg(60, stop="end", text="x", repeat_ratio=0.2)])
        self.assertNotIn("degeneration", _kinds(s))

    def test_output_truncated(self) -> None:
        s = df.session(messages=[df.msg(5, stop="length", text="partial")])
        self.assertIn("output-truncated", _kinds(s))


class GenerationStallTest(unittest.TestCase):
    def _slow_session(self, final_gap: float, step: float = 2.5):
        messages = [df.msg(1, "user", text="go", at=df.ts(0))]
        tools = []
        clock, line = 0.0, 2
        for _ in range(4):
            clock += step
            messages.append(df.msg(line, text="step", at=df.ts(clock), stop="tool_use",
                                   tool_calls=1))
            tools.append(df.call(line, at=df.ts(clock), result_at=df.ts(clock + 0.1)))
            clock += 0.1
            line += 2
        messages.append(df.msg(line, at=df.ts(clock + final_gap), stop="aborted"))
        return df.session(messages=messages, tools=tools), line

    def test_an_outlier_turn_fires(self) -> None:
        s, line = self._slow_session(final_gap=18)
        stalls = [a for a in detect(s) if a.kind == "generation-stall"]
        self.assertEqual([a.lines for a in stalls], [[line]])

    def test_a_uniformly_slow_model_does_not_fire(self) -> None:
        s, _line = self._slow_session(final_gap=12, step=12)
        self.assertNotIn("generation-stall", _kinds(s))

    def test_unusable_timestamps_are_skipped(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go", at=""),
                                 df.msg(2, at="garbage"), df.msg(3, at="")])
        self.assertEqual(generation.assistant_latencies(s), [])
        self.assertNotIn("generation-stall", _kinds(s))

    def test_claude_code_block_records_do_not_fire(self) -> None:
        """Claude Code block records with normal gaps don't spuriously fire."""
        messages = [df.msg(1, "user", text="go", at=df.ts(0))]
        tools = []
        line = 2
        clock = 0.0
        for _ in range(4):
            clock += 90  # 1.5 minutes between turns
            # First block of turn (intermediate, no stop_reason)
            messages.append(df.msg(line, stop="", reasoning_len=100,
                                   at=df.ts(clock)))
            line += 1
            # Second block (final with tool call)
            messages.append(df.msg(line, stop="tool_use", tool_calls=1,
                                   at=df.ts(clock + 0.1)))
            tools.append(df.call(line, at=df.ts(clock + 0.1),
                                result_at=df.ts(clock + 0.2)))
            line += 1
        s = df.session("claude-code", messages=messages, tools=tools)
        self.assertNotIn("generation-stall", _kinds(s))

    def test_claude_code_real_gap_before_first_block_fires(self) -> None:
        """A large gap before Claude Code's first block record fires stall."""
        messages = [df.msg(1, "user", text="go", at=df.ts(0))]
        tools = []
        line = 2
        clock = 0.0
        stalled_line = None
        for turn in range(4):
            if turn == 2:
                clock += 1200  # 20 minutes before turn 3's first block
            else:
                clock += 90  # 1.5 minutes for other turns
            # First block of turn (intermediate, no stop_reason)
            messages.append(df.msg(line, stop="", reasoning_len=100,
                                   at=df.ts(clock)))
            if turn == 2:
                stalled_line = line  # Mark this as the stalled message
            line += 1
            # Second block (final with tool call)
            messages.append(df.msg(line, stop="tool_use", tool_calls=1,
                                   at=df.ts(clock + 0.1)))
            tools.append(df.call(line, at=df.ts(clock + 0.1),
                                result_at=df.ts(clock + 0.2)))
            line += 1
        s = df.session("claude-code", messages=messages, tools=tools)
        stalls = [a for a in detect(s) if a.kind == "generation-stall"]
        self.assertEqual(len(stalls), 1)
        self.assertEqual(stalls[0].lines, [stalled_line])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
