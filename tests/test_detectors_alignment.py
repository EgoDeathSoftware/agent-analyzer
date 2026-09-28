"""Alignment candidates: did the work match the ask?"""

from __future__ import annotations

import unittest

from sessionkit.detectors import alignment
from tests import detector_fixtures as df


class ScopeCreepTest(unittest.TestCase):
    def _session(self, ask: str):
        return df.session(messages=[df.msg(1, "user", text=ask)],
                          tools=[df.call(2, "read", {"path": "/a.py"}),
                                 df.call(3, "edit", {"path": "/a.py"})])

    def test_review_ask_with_edits(self) -> None:
        s = self._session("review the parser, don't change anything")
        self.assertEqual([a.lines for a in alignment.scope_creep(s)], [[1, 3]])

    def test_an_implement_ask_is_not_scope_creep(self) -> None:
        self.assertEqual(alignment.scope_creep(self._session("review and fix the parser")), [])


class UnverifiedCompletionTest(unittest.TestCase):
    def _session(self, *commands: str):
        tools = [df.call(2, "edit", {"path": "/a.py"})]
        tools += [df.call(3 + i, "bash", {"command": c}) for i, c in enumerate(commands)]
        final = df.msg(20, text="All tests pass.", claims_done=True)
        return df.session(tools=tools, messages=[df.msg(1, "user", text="fix it"), final],
                          final_text="All tests pass.")

    def test_claim_with_no_verification(self) -> None:
        found = alignment.unverified_completion(self._session("git diff"))
        self.assertEqual([a.lines for a in found], [[2, 20]])

    def test_claim_after_running_tests(self) -> None:
        self.assertEqual(alignment.unverified_completion(self._session("pytest -q")), [])

    def test_no_changes_means_nothing_to_verify(self) -> None:
        s = df.session(messages=[df.msg(5, text="Done.", claims_done=True)], final_text="Done.")
        self.assertEqual(alignment.unverified_completion(s), [])


class PrematureStopTest(unittest.TestCase):
    def test_ends_on_a_stated_next_step(self) -> None:
        s = df.session(messages=[df.msg(1, "user", text="go"),
                                 df.msg(4, text="I'll run the tests next.")],
                       final_text="I'll run the tests next.", end_state="complete")
        self.assertEqual([a.lines for a in alignment.premature_stop(s)], [[4]])

    def test_a_finished_answer_is_not_premature(self) -> None:
        s = df.session(messages=[df.msg(4, text="Finished; all tests pass.")],
                       final_text="Finished; all tests pass.", end_state="complete")
        self.assertEqual(alignment.premature_stop(s), [])


class UserTurnsTest(unittest.TestCase):
    def test_turns_split_on_real_asks_only(self) -> None:
        s = df.session("claude-code", messages=[
            df.msg(1, "user", text="first"),
            df.msg(4, "user", text="[Request interrupted by user]"),
            df.msg(6, "user", text="second")],
            tools=[df.call(2, "Read"), df.call(5, "Read"), df.call(7, "Read")])
        turns = alignment.user_turns(s)
        self.assertEqual([(t.ask.line, [c.line for c in t.calls]) for t in turns],
                         [(1, [2, 5]), (6, [7])])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
