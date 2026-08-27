"""Tests for reading settings.json / .claude.json."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sessionkit import settingsjson as sj


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write_settings(self, data: dict) -> None:
        (self.root / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    def test_missing_settings_file_returns_empty(self) -> None:
        self.assertEqual(sj.read_settings(self.root), {})

    def test_malformed_settings_file_returns_empty(self) -> None:
        (self.root / "settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(sj.read_settings(self.root), {})

    def test_hook_defs_extracts_echo_messages(self) -> None:
        self._write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command",
             "command": "if bad; then echo 'BLOCKED: use rg' >&2; exit 2; fi"},
        ]}]}})
        defs = sj.hook_defs(sj.read_settings(self.root))
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0].event, "PreToolUse")
        self.assertEqual(defs[0].matcher, "Bash")
        self.assertEqual(defs[0].messages, ["BLOCKED: use rg"])

    def test_hook_defs_handles_multiple_scripts_per_matcher(self) -> None:
        self._write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "echo 'BLOCKED: one' >&2; exit 2"},
            {"type": "command", "command": "echo 'BLOCKED: two' >&2; exit 2"},
        ]}]}})
        defs = sj.hook_defs(sj.read_settings(self.root))
        self.assertEqual([d.messages[0] for d in defs], ["BLOCKED: one", "BLOCKED: two"])

    def test_hook_defs_empty_when_no_hooks_key(self) -> None:
        self._write_settings({})
        self.assertEqual(sj.hook_defs(sj.read_settings(self.root)), [])

    def test_deny_rules_parsed_into_tool_pattern_pairs(self) -> None:
        self._write_settings({"permissions": {"deny": [
            "Bash(rm -rf *)", "Bash(git push --force*)", "Edit(~/.bashrc)",
        ]}})
        rules = sj.deny_rules(sj.read_settings(self.root))
        self.assertIn(("Bash", "rm -rf *"), rules)
        self.assertIn(("Bash", "git push --force*"), rules)
        self.assertIn(("Edit", "~/.bashrc"), rules)

    def test_deny_rules_skips_unparseable_entries(self) -> None:
        self._write_settings({"permissions": {"deny": ["not-a-rule-shape"]}})
        self.assertEqual(sj.deny_rules(sj.read_settings(self.root)), [])

    def test_cleanup_period_days(self) -> None:
        self._write_settings({"cleanupPeriodDays": 365})
        self.assertEqual(sj.cleanup_period_days(sj.read_settings(self.root)), 365)

    def test_cleanup_period_days_absent(self) -> None:
        self._write_settings({})
        self.assertIsNone(sj.cleanup_period_days(sj.read_settings(self.root)))

    def test_read_allowed_tools(self) -> None:
        claude_json = self.root / ".claude.json"
        claude_json.write_text(json.dumps({"projects": {
            "/home/dev/myproject": {"allowedTools": ["Bash(git *)"]},
        }}), encoding="utf-8")
        self.assertEqual(sj.read_allowed_tools(claude_json, "/home/dev/myproject"),
                         ["Bash(git *)"])

    def test_read_allowed_tools_missing_project(self) -> None:
        claude_json = self.root / ".claude.json"
        claude_json.write_text(json.dumps({"projects": {}}), encoding="utf-8")
        self.assertEqual(sj.read_allowed_tools(claude_json, "/nowhere"), [])

    def test_read_allowed_tools_missing_file(self) -> None:
        self.assertEqual(sj.read_allowed_tools(self.root / "nope.json", "/x"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
