"""Unit tests for classify.derive_tail_signal.

Every TAIL_SIGNALS value has one positive and one negative fixture. The final test pins the
precedence table so a later reordering fails loudly instead of quietly changing report ranks.
"""

from __future__ import annotations

import unittest

from sessionkit.classify import TAIL_SIGNALS, derive_tail_signal
from sessionkit.parse import Message, ParsedSession, ToolCall


def _session(*, messages: list[Message] | None = None,
             tools: list[ToolCall] | None = None) -> ParsedSession:
    return ParsedSession(sid="s", source_id="test", path="/x",
                         messages=list(messages or []), tools=list(tools or []))


def _msg(line: int, role: str, preview: str = "") -> Message:
    return Message(uuid=f"u{line}", line=line, ts="2026-01-01T00:00:00Z",
                   role=role, preview=preview, text_len=len(preview))


def _tool(line: int, name: str = "Bash", *, is_error: bool = False,
          err_class: str = "") -> ToolCall:
    return ToolCall(tool_use_id=f"t{line}", line=line, name=name, ts="2026-01-01T00:00:00Z",
                    is_error=is_error, err_class=err_class)


class TailSignalTest(unittest.TestCase):
    def test_precedence_order_is_pinned(self) -> None:
        self.assertEqual(TAIL_SIGNALS, (
            "mid_tool", "error_tail", "apology", "completion_marker",
            "next_step_stated", "silent", "neutral",
        ))

    def test_mid_tool_wins_over_completion_marker(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "The refactor is done and shipped.")],
            tools=[_tool(11, "Bash", is_error=False, err_class="no-result")],
        )
        self.assertEqual(derive_tail_signal(session, {10: "The refactor is done and shipped."}),
                         "mid_tool")

    def test_mid_tool_negative_when_all_tools_resolved(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "Neutral text.")],
            tools=[_tool(11, "Bash", is_error=False)],
        )
        self.assertEqual(derive_tail_signal(session, {10: "Neutral text."}), "neutral")

    def test_error_tail_when_final_tool_failed(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "Trying again.")],
            tools=[_tool(11, "Bash", is_error=True, err_class="exit-code")],
        )
        self.assertEqual(derive_tail_signal(session, {10: "Trying again."}), "error_tail")

    def test_error_tail_negative_when_no_result_is_the_tail(self) -> None:
        session = _session(
            messages=[_msg(10, "assistant", "Trying again.")],
            tools=[_tool(11, "Bash", is_error=False, err_class="no-result")],
        )
        self.assertEqual(derive_tail_signal(session, {10: "Trying again."}), "mid_tool")

    def test_apology_matches_last_assistant_message(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "I'm sorry, I can't proceed without more context.")])
        self.assertEqual(
            derive_tail_signal(session,
                               {10: "I'm sorry, I can't proceed without more context."}),
            "apology")

    def test_apology_negative_when_text_is_neutral(self) -> None:
        session = _session(messages=[_msg(10, "assistant", "All caught up.")])
        self.assertEqual(derive_tail_signal(session, {10: "All caught up."}), "neutral")

    def test_completion_marker_matches_final_assistant_text(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "The migration is complete and tests pass.")])
        self.assertEqual(
            derive_tail_signal(session,
                               {10: "The migration is complete and tests pass."}),
            "completion_marker")

    def test_completion_marker_negative_when_word_is_absent(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "Working on the next step now.")])
        self.assertNotEqual(
            derive_tail_signal(session, {10: "Working on the next step now."}),
            "completion_marker")

    def test_next_step_stated_matches_forward_statement(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "I'll refactor the parser next.")])
        self.assertEqual(
            derive_tail_signal(session, {10: "I'll refactor the parser next."}),
            "next_step_stated")

    def test_next_step_stated_negative_when_message_is_past_tense(self) -> None:
        session = _session(messages=[_msg(10, "assistant",
                                          "Refactored the parser.")])
        self.assertNotEqual(
            derive_tail_signal(session, {10: "Refactored the parser."}),
            "next_step_stated")

    def test_silent_when_final_turn_is_a_user_prompt(self) -> None:
        session = _session(messages=[_msg(10, "user", "any update?")])
        self.assertEqual(derive_tail_signal(session, {10: "any update?"}), "silent")

    def test_silent_negative_when_assistant_replied(self) -> None:
        session = _session(messages=[
            _msg(10, "user", "any update?"),
            _msg(11, "assistant", "yes, done."),
        ])
        self.assertEqual(derive_tail_signal(session,
                                            {10: "any update?", 11: "yes, done."}),
                         "completion_marker")

    def test_neutral_when_no_rule_matches(self) -> None:
        session = _session(messages=[_msg(10, "assistant", "Some ordinary text.")])
        self.assertEqual(derive_tail_signal(session, {10: "Some ordinary text."}), "neutral")

    def test_empty_session_is_neutral(self) -> None:
        self.assertEqual(derive_tail_signal(_session(), {}), "neutral")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
