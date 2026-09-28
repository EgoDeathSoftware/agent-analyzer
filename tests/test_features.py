"""Table-driven tests for the parse-time text features."""

from __future__ import annotations

import unittest

from sessionkit import features


class SplitContentTest(unittest.TestCase):
    def test_string_content_is_visible(self) -> None:
        self.assertEqual(features.split_content("hi", "tool_use"), ("hi", "", 0))

    def test_blocks_split_into_visible_reasoning_and_calls(self) -> None:
        blocks = [{"type": "thinking", "thinking": "hmm"},
                  {"type": "text", "text": "answer"},
                  {"type": "tool_use", "id": "t", "name": "Read", "input": {}}]
        self.assertEqual(features.split_content(blocks, "tool_use"), ("answer", "hmm", 1))

    def test_only_the_named_tool_block_type_counts(self) -> None:
        blocks = [{"type": "toolCall", "id": "c", "name": "read", "arguments": {}}]
        self.assertEqual(features.split_content(blocks, "toolCall")[2], 1)
        self.assertEqual(features.split_content(blocks, "tool_use")[2], 0)

    def test_non_list_content(self) -> None:
        self.assertEqual(features.split_content(None, "tool_use"), ("", "", 0))


class MarkupLeakTest(unittest.TestCase):
    CASES = [
        ('<tool_call>\n{"name": "read", "arguments": {}}\n</tool_call>', "tool_call-xml"),
        ("<function=read>\n<parameter=path>/a</parameter>\n</function>", "function-tag"),
        ("<|channel|>commentary to=functions.read <|constrain|>json", "harmony"),
        ('{"name": "bash", "arguments": {"command": "ls"}}', "json-call"),
        ('Use a call like {"name": "bash", "arguments": {}} to run it.', ""),
        ("```\n<tool_call>{}</tool_call>\n```", ""),
        ("", ""),
    ]

    def test_cases(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(text=text[:30]):
                self.assertEqual(features.markup_leak(text), expected)


class RepeatRatioTest(unittest.TestCase):
    def test_short_text_is_not_judged(self) -> None:
        self.assertEqual(features.repeat_ratio("one two three"), 0.0)

    def test_distinct_text_scores_zero(self) -> None:
        self.assertEqual(features.repeat_ratio(" ".join(f"w{i}" for i in range(200))), 0.0)

    def test_a_restated_plan_scores_high(self) -> None:
        sentence = "Now I am setting up the work profile settings file with the defaults. "
        self.assertGreater(features.repeat_ratio(sentence * 30), 0.8)

    def test_large_input_is_handled(self) -> None:
        text = "alpha beta gamma delta " * 3000
        self.assertGreater(features.repeat_ratio(text), 0.9)


class ExtractRefsTest(unittest.TestCase):
    def test_backticks_paths_and_filenames(self) -> None:
        refs = features.extract_refs(
            "implement @docs/superpowers/plans/x.md using `run_key` in harness.py")
        self.assertEqual(refs, ["run_key", "docs/superpowers/plans/x.md", "harness.py"])

    def test_urls_are_not_refs(self) -> None:
        self.assertEqual(features.extract_refs("see https://pi.dev/docs/latest/models"), [])

    def test_deduplicated_and_capped(self) -> None:
        text = " ".join(f"f{i}.py" for i in range(40)) + " f1.py"
        refs = features.extract_refs(text)
        self.assertEqual(len(refs), 30)
        self.assertEqual(len(set(refs)), 30)


class ClaimsDoneTest(unittest.TestCase):
    def test_claims(self) -> None:
        for text in ("All tests pass.", "The fix is done and verified.", "Done.",
                     "✅ implemented"):
            with self.subTest(text):
                self.assertTrue(features.claims_done(text))

    def test_not_claims(self) -> None:
        for text in ("Let me check if this works.", "Next I'll run the tests.", ""):
            with self.subTest(text):
                self.assertFalse(features.claims_done(text))


class TimestampTest(unittest.TestCase):
    def test_delta(self) -> None:
        self.assertEqual(features.delta_ms("2026-09-26T01:00:00.000Z",
                                           "2026-09-26T01:00:02.500Z"), 2500)

    def test_mixed_offset_formats(self) -> None:
        self.assertEqual(features.delta_ms("2026-09-26T01:00:00+00:00",
                                           "2026-09-26T01:00:01.000Z"), 1000)

    def test_unusable_timestamps(self) -> None:
        self.assertIsNone(features.delta_ms("", "2026-09-26T01:00:00Z"))
        self.assertIsNone(features.delta_ms("garbage", "2026-09-26T01:00:00Z"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
