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

    def test_a_fixing_gerund_ask_is_not_scope_creep(self) -> None:
        s = self._session("You are fixing two issues found in a review")
        self.assertEqual(alignment.scope_creep(s), [])


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

    def test_let_me_know_is_not_premature(self) -> None:
        s = df.session(messages=[df.msg(4, text="Let me know if you want more changes.")],
                       final_text="Let me know if you want more changes.",
                       end_state="complete")
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


class FocusProfileTest(unittest.TestCase):
    ASK = "fix the bug in sessionkit/parse.py"

    def _session(self, tools, ask: str = ASK, **kw):
        refs = ["sessionkit/parse.py"] if "sessionkit/parse.py" in ask else []
        return df.session(messages=[df.msg(1, "user", text=ask, refs=refs)], tools=tools, **kw)

    def test_consecutive_calls_on_one_key_form_one_run(self) -> None:
        s = self._session([df.call(2, "read", {"path": "/repo/sessionkit/parse.py"}),
                           df.call(3, "read", {"path": "/repo/sessionkit/corpus.py"}),
                           df.call(4, "bash", {"command": "rg foo"})])
        runs = alignment.focus_profile(s)[0].runs
        self.assertEqual([(r.key, r.lines) for r in runs],
                         [("dir:/repo/sessionkit", [2, 3]), ("exec:rg", [4])])
        self.assertTrue(runs[0].referenced)

    def test_a_large_unreferenced_run_is_a_drift_candidate(self) -> None:
        tools = [df.call(2, "read", {"path": "/repo/sessionkit/parse.py"}),
                 df.call(3, "edit", {"path": "/repo/sessionkit/parse.py"})]
        tools += [df.call(i, "read", {"path": f"/usr/lib/node_modules/pkg/dist/{i}.js"})
                  for i in range(4, 12)]
        drift = [a for a in alignment.drift_candidates(self._session(tools))]
        self.assertEqual([a.lines for a in drift], [list(range(4, 12))])

    def test_asks_without_refs_produce_no_ref_based_candidates(self) -> None:
        tools = [df.call(i, "read", {"path": f"/usr/lib/pkg/{i}.js"}) for i in range(2, 12)]
        s = self._session(tools, ask="make the profiles switchable")
        self.assertEqual(alignment.drift_candidates(s), [])

    def test_a_user_interrupt_marks_the_calls_before_it(self) -> None:
        tools = [df.call(i, "bash", {"command": f"rg x{i}"}) for i in range(2, 32, 2)]
        s = df.session(messages=[df.msg(1, "user", text="set up two profiles"),
                                 df.msg(33, stop="aborted"),
                                 df.msg(34, "user", text="just write a script")], tools=tools)
        drift = alignment.drift_candidates(s)
        self.assertEqual([a.lines for a in drift], [[16, 18, 20, 22, 24, 26, 28, 30]])

    def test_judgment_kinds_are_registered(self) -> None:
        from sessionkit import taxonomy
        for name in ("false-premise", "spec-neglect", "misread-output",
                     "ignored-own-warning", "role-drift"):
            with self.subTest(name):
                self.assertEqual(taxonomy.lookup(name).tier, taxonomy.TIER_J)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
