"""Tests for the failure taxonomy table itself: names, tiers, families, hints."""

from __future__ import annotations

import unittest

from sessionkit import taxonomy

VALID_FAMILIES = {"generic", "tool-protocol", "generation", "loop", "lifecycle", "recovery",
                  "alignment", "environment"}
VALID_TIERS = {taxonomy.TIER_S, taxonomy.TIER_CANDIDATE, taxonomy.TIER_J}
VALID_TARGETS = {"model", "harness", "serving", "env", "none"}


class TaxonomyTableTest(unittest.TestCase):
    """The table is well formed: unique names, valid enums, a hint everywhere."""

    def test_names_are_unique_across_classes_and_kinds(self) -> None:
        names = [c.name for c in taxonomy.ALL_CLASSES]
        self.assertEqual(len(names), len(set(names)))

    def test_every_class_is_well_formed(self) -> None:
        for entry in taxonomy.ALL_CLASSES:
            with self.subTest(entry.name):
                self.assertIn(entry.family, VALID_FAMILIES)
                self.assertIn(entry.tier, VALID_TIERS)
                self.assertIn(entry.fix_target, VALID_TARGETS)
                self.assertTrue(entry.hint)

    def test_error_classes_carry_patterns(self) -> None:
        for entry in taxonomy.ERROR_CLASSES:
            with self.subTest(entry.name):
                self.assertTrue(entry.patterns)

    def test_lookup(self) -> None:
        self.assertEqual(taxonomy.lookup("repeat-tool").family, "loop")
        self.assertIsNone(taxonomy.lookup("no-such-kind"))

    def test_killed_is_registered_for_the_exit_code_mapping(self) -> None:
        self.assertEqual(taxonomy.classify_error("Exit code 137")[0], "killed")
        self.assertIsNotNone(taxonomy.lookup("killed"))


class ErrorClassTest(unittest.TestCase):
    """Exact strings from pi 0.84.1's source and from the local Claude Code corpus."""

    CASES = [
        ('{"ok": false, "errors": [["description", "too short"]]}',
         "tool-error-structured"),
        ('Validation failed for tool "edit":\n  - path: required\n\nReceived arguments:\n{}',
         "args-dropped"),
        ('Validation failed for tool "edit":\n  - edits: required\n\nReceived arguments:\n'
         '{"path": "/a"}', "invalid-args"),
        ("MCP error -32602: Input validation error: Invalid arguments for tool",
         "invalid-args"),
        ("Tool bash_exec not found", "unknown-tool"),
        ("Unknown skill: brainstorm", "unknown-tool"),
        ('Tool call "write" was not executed: the response hit the output token limit, so '
         "its arguments may be truncated.", "truncated-call"),
        ("Could not find the exact text in /a.py. The old text must match exactly including "
         "all whitespace and newlines.", "edit-mismatch"),
        ("Found 3 occurrences of the text in /a.py. The text must be unique.",
         "edit-mismatch"),
        ("<tool_use_error>String to replace not found in file.", "edit-mismatch"),
        ("edits[0] and edits[1] overlap in /a.py. Merge them into one edit.",
         "edit-mismatch"),
        ("EISDIR: illegal operation on a directory, read", "tool-misuse"),
        ("Offset 900 is beyond end of file (120 lines total)", "tool-misuse"),
        ("Sibling tool call errored", "sibling-cascade"),
        ("[Request interrupted by user for tool use]", "aborted"),
        ("partial output\n\nCommand aborted", "aborted"),
        ("partial output Command aborted", "aborted"),
        ("Operation aborted", "aborted"),
        ("AbortError: Operation aborted\n    at Timeout.run (/srv/app/x.js:1:1)\n"
         "fetch timed out after 30000ms", "timeout"),
        ("This session is isolated in the worktree /tmp/wt/feature, but this command writes "
         "outside it.", "policy-denied"),
        ("File is in a directory that is denied by your permission settings.",
         "policy-denied"),
        ("Tool execution was blocked", "policy-denied"),
        ("Denied by deny-list: no widgets on fridays", "policy-denied"),
        ("Denied by deny-list: no widgets on fridays\n\nCommand exited with code 1",
         "policy-denied"),
        ("The user doesn't want to take this action right now.", "user-rejected"),
        ("MCP error: No active transport", "mcp-error"),
        ("some output\n\nCommand exited with code 1", "exit-code"),
        ("killed\n\nCommand exited with code 137", "killed"),
        ("bash: foo: command not found\n\nCommand exited with code 127", "missing-tool"),
        ("Command timed out after 30 seconds", "timeout"),
    ]

    def test_cases(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(expected=expected, text=text[:40]):
                self.assertEqual(taxonomy.classify_error(text)[0], expected)


class ApiErrorTest(unittest.TestCase):
    """Model-call failures recorded on the assistant message, not on a tool result."""

    CASES = [
        ("rate_limit: You've hit your session limit · resets 6:40pm", "rate-limit"),
        ("authentication_failed: Please run /login · API Error: 401", "auth"),
        ("OAuth refresh failed for anthropic: Refresh token expired", "auth"),
        ("server_error: API Error: 529 Overloaded.", "api-error"),
        ('400: {"message":"This model\'s maximum context length is 131072 tokens."}',
         "context-overflow"),
        ("Request timed out.", "network"),
        ("Provider finish_reason: content_filter", "api-error"),
        ("", ""),
    ]

    def test_cases(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(expected=expected, text=text[:40]):
                self.assertEqual(taxonomy.classify_api_error(text), expected)

    def test_api_classes_are_registered(self) -> None:
        for name in ("context-overflow", "auth", "network"):
            self.assertIsNotNone(taxonomy.lookup(name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
