"""Recovery candidates: failures the agent ignored or gave up on."""

from __future__ import annotations

import unittest

from sessionkit.detectors import recovery
from tests import detector_fixtures as df


class ErrorIgnoredTest(unittest.TestCase):
    def test_failure_then_a_success_claim(self) -> None:
        s = df.session(tools=[df.call(2, is_error=True, err_class="exit-code", result_line=3)],
                       messages=[df.msg(4, text="All tests pass.", claims_done=True)])
        self.assertEqual([a.lines for a in recovery.error_ignored(s)], [[2, 4]])

    def test_a_retry_before_the_claim_is_not_ignoring(self) -> None:
        s = df.session(tools=[df.call(2, is_error=True, err_class="exit-code", result_line=3),
                              df.call(4, result_line=5)],
                       messages=[df.msg(6, text="All tests pass.", claims_done=True)])
        self.assertEqual(recovery.error_ignored(s), [])

    def test_a_user_abort_is_not_the_agents_failure(self) -> None:
        s = df.session(tools=[df.call(2, is_error=True, err_class="aborted", result_line=3)],
                       messages=[df.msg(4, text="Done.", claims_done=True)])
        self.assertEqual(recovery.error_ignored(s), [])

    def test_claude_code_split_message_cites_the_claiming_record(self) -> None:
        """One API message split into a thinking-only record (no claim) and a text record
        (the claim), sharing one msg_id — the finding cites the text record's line."""
        s = df.session(tools=[df.call(2, is_error=True, err_class="exit-code", result_line=3)],
                       messages=[df.msg(4, msg_id="m1", claims_done=False),
                                df.msg(5, msg_id="m1", text="All tests pass.",
                                       claims_done=True)])
        self.assertEqual([a.lines for a in recovery.error_ignored(s)], [[2, 5]])


class GiveUpTest(unittest.TestCase):
    def _session(self, final_text: str, errors: int = 1):
        tools = [df.call(i, is_error=True, err_class="exit-code", result_line=i)
                 for i in range(2, 2 + errors)]
        final = df.msg(10, text=final_text)
        return df.session(tools=tools, messages=[df.msg(1, "user", text="go"), final],
                          final_text=final_text, end_state="complete")

    def test_one_failure_then_an_apology(self) -> None:
        s = self._session("Sorry, I cannot get this command to work.")
        self.assertEqual([a.kind for a in recovery.give_up(s)], ["give-up"])

    def test_a_cascade_is_not_giving_up_after_one_failure(self) -> None:
        self.assertEqual(recovery.give_up(self._session("Sorry.", errors=3)), [])

    def test_a_completion_claim_is_not_giving_up(self) -> None:
        self.assertEqual(recovery.give_up(self._session("Done, it works now.")), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
