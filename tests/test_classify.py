"""Tests for the error taxonomy, end-state derivation and anomaly detectors.

Every detector gets a positive **and** a negative case: a detector that can never return false
is not tested, it is just asserted.
"""

from __future__ import annotations

import unittest

from sessionkit.classify import (annotate_errors, classify_error, derive_end_state, detect,
                                 signature)
from sessionkit.parse import FileOp, ParsedSession, SysEvent, ToolCall


def _session(**kw) -> ParsedSession:
    """A blank session with the given fields overridden."""
    return ParsedSession(sid="s", source_id="host", path="/tmp/s.jsonl", **kw)


def _call(line: int, name: str = "Bash", is_error: bool = False, out: str = "",
          err_class: str = "", digest_: str = "d0", dur: int | None = None) -> ToolCall:
    """A tool call with the fields the detectors read."""
    return ToolCall(tool_use_id=f"t{line}", line=line, name=name, ts="", is_error=is_error,
                    output_preview=out, err_class=err_class, input_digest=digest_, dur_ms=dur)


class TaxonomyTest(unittest.TestCase):
    """classify_error over real corpus strings."""

    CASES = [
        ("PreToolUse:Bash hook error: [...]: BLOCKED: Use rg instead", "hook-block"),
        ("The user doesn't want to proceed with this tool use.", "user-rejected"),
        ("File content (128804 tokens) exceeds maximum allowed tokens (25000).",
         "file-too-large"),
        ("Exit code 1 (eval):1: command not found: uv", "missing-tool"),
        ("EACCES: permission denied, open '/x'", "permission-denied"),
        ("<tool_use_error>Directory does not exist: /workspace/x", "not-found"),
        ("Error in brave_web_search: Rate limit exceeded: Too Many Requests", "rate-limit"),
        ("Exit code 128 fatal: not a git repository", "not-a-repo"),
        ("Request failed with status code 400", "api-error"),
        ("<tool_use_error>File has been modified since read", "stale-read"),
        ("<tool_use_error>File has not been read yet. Read it first before writing.",
         "unread-file"),
        ("Exit code 2", "exit-code"),
        ("Exit code 124", "timeout"),
        ("Exit code 137", "killed"),
        ("<tool_use_error>InputValidationError: Read failed due to the following issue: "
         "The parameter `offset` type is expected as `number` but provided as `string`",
         "input-validation"),
        ("Permission for this action was denied by the Claude Code auto mode classifier. "
         "Reason: [Merge Without Review]", "policy-denied"),
        ("Permission to use Bash with command sudo cp /a /b has been denied.",
         "policy-denied"),
    ]

    def test_known_classes(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(expected):
                self.assertEqual(classify_error(text)[0], expected)

    def test_unmatched_text_is_other_not_a_false_match(self) -> None:
        self.assertEqual(classify_error("something entirely novel happened")[0], "other")

    def test_empty_body(self) -> None:
        self.assertEqual(classify_error("")[0], "empty")

    def test_permission_denied_wins_over_exit_code(self) -> None:
        """Ordering matters: a denied mkdir arrives wrapped in an exit code."""
        text = "Exit code 1 mkdir: cannot create directory '/x': Permission denied"
        self.assertEqual(classify_error(text)[0], "permission-denied")

    def test_classes_carry_a_fix_hint(self) -> None:
        self.assertTrue(classify_error(self.CASES[0][0])[1])

    def test_known_exit_code_wins_over_incidental_body_text(self) -> None:
        """The exit code is the tool's verdict on the call; body text is often incidental.

        A compound command that timed out can still contain a stray ENOENT from one of its
        earlier parts. Classifying that as ``not-found`` sends the reader after the wrong
        failure.
        """
        text = ("Exit code 124 ls: cannot access 'server/src/x.ts': "
                "No such file or directory")
        self.assertEqual(classify_error(text)[0], "timeout")

    def test_unmapped_exit_code_stays_generic(self) -> None:
        """Only codes with an unambiguous meaning are special-cased."""
        self.assertEqual(classify_error("Exit code 144")[0], "exit-code")

    def test_policy_denial_is_not_user_rejection(self) -> None:
        """A harness block and a human declining are different problems with different fixes."""
        policy = classify_error("Permission for this action was denied by the Claude Code "
                                "auto mode classifier.")
        human = classify_error("The user doesn't want to proceed with this tool use.")
        self.assertEqual(policy[0], "policy-denied")
        self.assertEqual(human[0], "user-rejected")
        self.assertNotEqual(policy[1], human[1])

    def test_signature_normalises_variable_parts(self) -> None:
        a = signature("Exit code 1 at /home/alice/project/file.py line 44")
        b = signature("Exit code 7 at /home/bob/other/thing.py line 9")
        self.assertEqual(a, b)

    def test_signature_keeps_distinct_errors_distinct(self) -> None:
        self.assertNotEqual(signature("Exit code 1"), signature("Permission denied"))


class EndStateTest(unittest.TestCase):
    """derive_end_state across the outcomes the corpus contains."""

    def test_complete(self) -> None:
        session = _session()
        session.messages.append(_msg("assistant"))
        self.assertEqual(derive_end_state(session)[0], "complete")

    def test_interrupted_tool(self) -> None:
        session = _session()
        session.tools.append(_call(1, err_class="no-result"))
        state, reason = derive_end_state(session)
        self.assertEqual(state, "interrupted-tool")
        self.assertIn("Bash", reason)

    def test_killed_agents(self) -> None:
        session = _session()
        session.sysev.append(SysEvent(1, "", "agents_killed"))
        self.assertEqual(derive_end_state(session)[0], "killed-agents")

    def test_error_cascade(self) -> None:
        session = _session()
        session.tools.extend(_call(i, is_error=True) for i in range(3))
        self.assertEqual(derive_end_state(session)[0], "error-cascade")

    def test_interrupted_user(self) -> None:
        session = _session()
        session.messages.append(_msg("user"))
        self.assertEqual(derive_end_state(session)[0], "interrupted-user")

    def test_unknown_when_empty(self) -> None:
        self.assertEqual(derive_end_state(_session())[0], "unknown")


class DetectorTest(unittest.TestCase):
    """Each detector fires on its pattern and stays silent otherwise."""

    def test_repeat_tool(self) -> None:
        session = _session()
        session.tools.extend(_call(i, digest_="same") for i in range(3))
        self.assertIn("repeat-tool", _kinds(session))

    def test_repeat_tool_negative(self) -> None:
        session = _session()
        session.tools.extend(_call(i, digest_=f"d{i}") for i in range(5))
        self.assertNotIn("repeat-tool", _kinds(session))

    def test_file_thrash(self) -> None:
        session = _session()
        session.files.extend(FileOp("/a.py", "edit", "", "t", line=i) for i in range(1, 6))
        anomaly = next(a for a in detect(session) if a.kind == "file-thrash")
        self.assertEqual(anomaly.lines, [1, 2, 3, 4, 5])

    def test_file_thrash_negative(self) -> None:
        session = _session()
        session.files.extend(FileOp(f"/{i}.py", "edit", "", "t", line=i) for i in range(9))
        self.assertNotIn("file-thrash", _kinds(session))

    def test_read_loop(self) -> None:
        session = _session()
        session.files.extend(FileOp("/a.py", "read", "", "t", line=i) for i in range(1, 5))
        anomaly = next(a for a in detect(session) if a.kind == "read-loop")
        self.assertEqual(anomaly.lines, [1, 2, 3, 4])

    def test_error_cascade_run(self) -> None:
        session = _session()
        session.tools.extend(_call(i, is_error=True) for i in range(3))
        session.tools.append(_call(9))
        self.assertIn("error-cascade", _kinds(session))

    def test_error_cascade_negative_when_interleaved(self) -> None:
        session = _session()
        for i in range(6):
            session.tools.append(_call(i, is_error=(i % 2 == 0)))
        self.assertNotIn("error-cascade", _kinds(session))

    def test_hook_pingpong(self) -> None:
        session = _session()
        session.tools.extend(_call(i, err_class="hook-block") for i in range(2))
        self.assertIn("hook-pingpong", _kinds(session))

    def test_hook_pingpong_negative_single_block(self) -> None:
        session = _session()
        session.tools.append(_call(1, err_class="hook-block"))
        self.assertNotIn("hook-pingpong", _kinds(session))

    def test_orphan_subagent(self) -> None:
        session = _session()
        session.tools.append(_call(1, name="Agent", err_class="no-result"))
        self.assertIn("orphan-subagent", _kinds(session))

    def test_compaction_churn(self) -> None:
        session = _session()
        session.sysev.extend(SysEvent(i, "", "compact_boundary") for i in range(2))
        self.assertIn("compaction-churn", _kinds(session))

    def test_compaction_churn_negative_single(self) -> None:
        session = _session()
        session.sysev.append(SysEvent(1, "", "compact_boundary"))
        self.assertNotIn("compaction-churn", _kinds(session))

    def test_stall(self) -> None:
        session = _session()
        session.tools.append(_call(1, dur=200_000))
        self.assertIn("stall", _kinds(session))

    def test_stall_negative_fast_call(self) -> None:
        session = _session()
        session.tools.append(_call(1, dur=50))
        self.assertNotIn("stall", _kinds(session))

    def test_rejection_persist(self) -> None:
        session = _session()
        session.tools.append(_call(1, err_class="user-rejected", digest_="x"))
        session.tools.append(_call(2, digest_="x"))
        self.assertIn("rejection-persist", _kinds(session))

    def test_rejection_persist_negative_when_not_retried(self) -> None:
        session = _session()
        session.tools.append(_call(1, err_class="user-rejected", digest_="x"))
        session.tools.append(_call(2, digest_="y"))
        self.assertNotIn("rejection-persist", _kinds(session))

    def test_clean_session_has_no_anomalies(self) -> None:
        session = _session()
        session.tools.append(_call(1, digest_="a"))
        session.files.append(FileOp("/a.py", "read", "", "t", line=1))
        self.assertEqual(detect(session), [])


class AnnotateTest(unittest.TestCase):
    """annotate_errors fills classes in place."""

    def test_annotates_only_failures(self) -> None:
        session = _session()
        session.tools.append(_call(1, is_error=True, out="BLOCKED: Use rg instead of grep"))
        session.tools.append(_call(2))
        annotate_errors(session)
        self.assertEqual(session.tools[0].err_class, "hook-block")
        self.assertEqual(session.tools[1].err_class, "")


class AnomalyHintTest(unittest.TestCase):
    """Every detected anomaly kind has a canned prevention hint (PLAN.md §7 Phase 2)."""

    def test_every_detector_kind_has_a_hint(self) -> None:
        from sessionkit.classify import ANOMALY_HINTS
        known_kinds = {"repeat-tool", "file-thrash", "read-loop", "error-cascade",
                      "hook-pingpong", "agent-kill", "orphan-subagent", "compaction-churn",
                      "stall", "rejection-persist"}
        self.assertEqual(set(ANOMALY_HINTS), known_kinds)
        self.assertTrue(all(ANOMALY_HINTS.values()), "every hint must be non-empty")


def _kinds(session: ParsedSession) -> list[str]:
    """Anomaly kinds detected for a session."""
    return [a.kind for a in detect(session)]


def _msg(role: str):
    """A minimal message for end-state tests."""
    from sessionkit.parse import Message
    return Message(uuid="u", line=1, ts="", role=role)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
