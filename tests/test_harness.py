"""Tests for per-harness tool roles and argument extraction."""

from __future__ import annotations

import json
import unittest

from sessionkit import harness
from sessionkit.parse import ToolCall


def _call(name: str, args: object = None, raw: str | None = None) -> ToolCall:
    preview = raw if raw is not None else json.dumps(args if args is not None else {})
    return ToolCall(tool_use_id="t", line=1, name=name, ts="", input_preview=preview)


class RoleTest(unittest.TestCase):
    def test_claude_code_roles(self) -> None:
        self.assertEqual(harness.role("claude-code", "Read"), "read")
        self.assertEqual(harness.role("claude-code", "Bash"), "exec")
        self.assertEqual(harness.role("claude-code", "Agent"), "dispatch")
        self.assertEqual(harness.role("claude-code", "AskUserQuestion"), "ask-user")

    def test_pi_roles(self) -> None:
        self.assertEqual(harness.role("pi", "read"), "read")
        self.assertEqual(harness.role("pi", "grep"), "search")
        self.assertEqual(harness.role("pi", "bash"), "exec")
        self.assertEqual(harness.role("pi", "subagent"), "dispatch")
        self.assertEqual(harness.role("pi", "bg_wait"), "poll")

    def test_unknown_harness_falls_back_to_every_table(self) -> None:
        self.assertEqual(harness.role("", "Edit"), "edit")
        self.assertEqual(harness.role("", "bash"), "exec")

    def test_unknown_tool_is_other(self) -> None:
        self.assertEqual(harness.role("pi", "mcp__server__tool"), "other")


class PathTest(unittest.TestCase):
    def test_path_from_json_args(self) -> None:
        call = _call("Read", {"file_path": "/a/b.py"})
        self.assertEqual(harness.call_paths("claude-code", call), ["/a/b.py"])

    def test_path_from_truncated_preview(self) -> None:
        raw = ('{"path": "/a/b.py", "content": "' + "x" * 3000)[:2000]
        self.assertEqual(harness.call_paths("pi", _call("write", raw=raw)), ["/a/b.py"])

    def test_exec_paths_from_command(self) -> None:
        call = _call("bash", {"command": "cd /repo && cat docs/plan.md | head"})
        self.assertEqual(harness.call_paths("pi", call), ["/repo", "docs/plan.md"])

    def test_exec_command_recovered_from_truncated_preview(self) -> None:
        call = _call("bash", raw='{"command": "rg -n foo /src/app.py')
        self.assertEqual(harness.command_of(call), "rg -n foo /src/app.py")
        self.assertEqual(harness.call_paths("pi", call), ["/src/app.py"])

    def test_raw_bash_execution_command(self) -> None:
        self.assertEqual(harness.command_of(_call("bash", raw="ls -la /tmp")), "ls -la /tmp")

    def test_raw_brace_group_command_is_not_mistaken_for_json(self) -> None:
        call = _call("bash", raw="{ make; } 2>&1")
        self.assertEqual(harness.command_of(call), "{ make; } 2>&1")

    def test_truncated_multiline_command_is_unescaped(self) -> None:
        raw = r'{"command": "cd /repo\npytest tests/a.py'
        call = _call("bash", raw=raw)
        self.assertEqual(harness.command_of(call), "cd /repo\npytest tests/a.py")
        self.assertEqual(harness.call_paths("pi", call), ["/repo", "tests/a.py"])

    def test_urls_and_flags_are_not_paths(self) -> None:
        call = _call("bash", {"command": "curl -s https://x.dev/a/b -o out.json"})
        self.assertEqual(harness.call_paths("pi", call), ["out.json"])


class ProgramTest(unittest.TestCase):
    def test_skips_cd_env_assignments_and_wrappers(self) -> None:
        self.assertEqual(harness.command_program("cd /repo && git status"), "git")
        self.assertEqual(harness.command_program("PKG=$(npm root -g)/x; rg -l foo $PKG"), "rg")
        self.assertEqual(harness.command_program("sudo docker run x"), "docker")
        self.assertEqual(harness.command_program("/usr/local/bin/llama-swap --help"),
                         "llama-swap")

    def test_multiline_command_splits_on_newline(self) -> None:
        self.assertEqual(harness.command_program("cd /repo\ngit status"), "git")

    def test_run_key(self) -> None:
        self.assertEqual(harness.run_key("pi", _call("bash", {"command": "rg x"})), "exec:rg")
        self.assertEqual(harness.run_key("claude-code", _call("Read", {"file_path": "/a/b.py"})),
                         "dir:/a")
        self.assertEqual(harness.run_key("claude-code", _call("Skill", {})), "other:Skill")


class TimeoutTest(unittest.TestCase):
    def test_pi_timeout_is_seconds(self) -> None:
        call = _call("bash", {"command": "x", "timeout": 30})
        self.assertEqual(harness.declared_timeout_ms("pi", call), 30_000)

    def test_claude_code_timeout_is_milliseconds(self) -> None:
        call = _call("Bash", {"command": "x", "timeout": 600_000})
        self.assertEqual(harness.declared_timeout_ms("claude-code", call), 600_000)

    def test_absent_or_invalid_timeout(self) -> None:
        self.assertIsNone(harness.declared_timeout_ms("pi", _call("bash", {"command": "x"})))
        self.assertIsNone(harness.declared_timeout_ms("pi", _call("bash", {"timeout": True})))
        self.assertIsNone(harness.declared_timeout_ms("pi", _call("bash", {"timeout": -1})))


class VerifyTest(unittest.TestCase):
    def test_verification_commands(self) -> None:
        for command in ("PYTHONPATH=. python3 -m unittest discover", "pytest -q",
                        "cargo test", "npm run test", "make check", "bash -n x.sh"):
            with self.subTest(command):
                self.assertTrue(harness.is_verify_command(command))

    def test_non_verification_commands(self) -> None:
        for command in ("ls -la", "cat Makefile", "git status"):
            with self.subTest(command):
                self.assertFalse(harness.is_verify_command(command))


class InterruptMarkerTest(unittest.TestCase):
    def test_claude_code_interrupt_marker(self) -> None:
        self.assertTrue(harness.is_interrupt_marker("[Request interrupted by user for tool use]"))
        self.assertFalse(harness.is_interrupt_marker("please interrupt the build"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
