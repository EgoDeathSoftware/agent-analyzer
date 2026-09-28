"""Loop detectors: new kinds and the retuned thresholds."""

from __future__ import annotations

import unittest

from sessionkit.detectors import detect
from tests import detector_fixtures as df


def _kinds(session) -> list[str]:
    return [a.kind for a in detect(session)]


class RepeatToolTest(unittest.TestCase):
    def test_polling_is_not_a_repeat_loop(self) -> None:
        s = df.session(tools=[df.call(i, "bg_wait", digest="same") for i in range(1, 6)])
        self.assertNotIn("repeat-tool", _kinds(s))

    def test_identical_commands_still_are(self) -> None:
        s = df.session(tools=[df.call(i, "bash", digest="same") for i in range(1, 4)])
        self.assertIn("repeat-tool", _kinds(s))


class EditMismatchSpiralTest(unittest.TestCase):
    def test_three_failures_on_one_file(self) -> None:
        s = df.session(tools=[df.call(i, "edit", {"path": "/a.py"}, is_error=True,
                                      err_class="edit-mismatch") for i in (2, 4, 6)])
        spiral = [a for a in detect(s) if a.kind == "edit-mismatch-spiral"]
        self.assertEqual([(a.detail, a.lines) for a in spiral], [("/a.py", [2, 4, 6])])

    def test_failures_spread_over_files_are_not(self) -> None:
        s = df.session(tools=[df.call(i, "edit", {"path": f"/{i}.py"}, is_error=True,
                                      err_class="edit-mismatch") for i in (2, 4, 6)])
        self.assertNotIn("edit-mismatch-spiral", _kinds(s))


class ReadOnlySpiralTest(unittest.TestCase):
    def test_long_run_of_reads_and_searches(self) -> None:
        tools = [df.call(i, "read" if i % 2 else "grep", {"path": f"/{i}.py"})
                 for i in range(1, 9)]
        self.assertIn("read-only-spiral", _kinds(df.session(tools=tools)))

    def test_an_edit_breaks_the_run(self) -> None:
        tools = [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(1, 5)]
        tools.append(df.call(5, "edit", {"path": "/1.py"}))
        tools += [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(6, 10)]
        self.assertNotIn("read-only-spiral", _kinds(df.session(tools=tools)))

    def test_a_substantial_answer_breaks_the_run(self) -> None:
        tools = [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(1, 5)]
        tools += [df.call(i, "read", {"path": f"/{i}.py"}) for i in range(6, 10)]
        messages = [df.msg(5, text="x" * 250, stop="tool_use", tool_calls=1)]
        self.assertNotIn("read-only-spiral",
                         _kinds(df.session(tools=tools, messages=messages)))


class FileThrashTest(unittest.TestCase):
    def test_five_edits_with_two_failures(self) -> None:
        tools = [df.call(i, "edit", is_error=i in (2, 3), tid=f"e{i}") for i in range(1, 6)]
        files = [df.FileOp("/a.py", "edit", "", f"e{i}", line=i) for i in range(1, 6)]
        self.assertIn("file-thrash", _kinds(df.session(tools=tools, files=files)))

    def test_seven_clean_edits_are_iterative_work(self) -> None:
        tools = [df.call(i, "edit", tid=f"e{i}") for i in range(1, 8)]
        files = [df.FileOp("/a.py", "edit", "", f"e{i}", line=i) for i in range(1, 8)]
        self.assertNotIn("file-thrash", _kinds(df.session(tools=tools, files=files)))


class ReadLoopTest(unittest.TestCase):
    def test_rereads_after_edits_and_compaction_do_not_count(self) -> None:
        files = [df.FileOp("/a.py", "read", "", "t", line=1),
                 df.FileOp("/a.py", "edit", "", "t", line=2),
                 df.FileOp("/a.py", "read", "", "t", line=3),
                 df.FileOp("/a.py", "read", "", "t", line=5),
                 df.FileOp("/a.py", "read", "", "t", line=6),
                 df.FileOp("/a.py", "read", "", "t", line=7)]
        sysev = [df.SysEvent(4, "", "compaction")]
        # Counted reads: 1, 6, 7 (3 is after an edit, 5 is after a compaction) → below 5.
        self.assertNotIn("read-loop", _kinds(df.session(files=files, sysev=sysev)))


class ErrorCascadeTest(unittest.TestCase):
    def test_sibling_cascades_do_not_count(self) -> None:
        tools = [df.call(1, is_error=True, err_class="exit-code"),
                 df.call(2, is_error=True, err_class="sibling-cascade"),
                 df.call(3, is_error=True, err_class="sibling-cascade"),
                 df.call(4)]
        self.assertNotIn("error-cascade", _kinds(df.session(tools=tools)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
