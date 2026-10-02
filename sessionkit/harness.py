"""Per-harness knowledge: which role each tool plays, and where its arguments keep paths.

Detectors key on roles (``read``, ``exec``, ``dispatch`` ...) instead of tool names, so a new
harness adds rows here instead of branches in every detector. Claude Code capitalises its tool
names and pi lowercases them, so the two tables never collide and a session with no recorded
harness can fall back to their union.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from sessionkit.parse import PATH_KEYS, ToolCall

ROLES = ("read", "search", "edit", "write", "exec", "dispatch", "poll", "ask-user", "other")

_TOOL_ROLES: dict[str, dict[str, str]] = {
    "claude-code": {
        "Read": "read", "NotebookRead": "read",
        "Grep": "search", "Glob": "search", "LS": "search", "WebSearch": "search",
        "WebFetch": "search", "ToolSearch": "search",
        "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
        "Write": "write",
        "Bash": "exec",
        "Agent": "dispatch", "Task": "dispatch",
        "TaskOutput": "poll", "BashOutput": "poll", "TaskGet": "poll", "TaskList": "poll",
        "ListAgents": "poll",
        "AskUserQuestion": "ask-user", "ExitPlanMode": "ask-user",
    },
    "pi": {
        "read": "read",
        "grep": "search", "find": "search", "ls": "search",
        "edit": "edit",
        "write": "write",
        "bash": "exec",
        "subagent": "dispatch",
        "bg_wait": "poll",
    },
}
_ANY_HARNESS: dict[str, str] = {
    name: tool_role for table in _TOOL_ROLES.values() for name, tool_role in table.items()
}

#: Harnesses whose ``timeout`` argument is in seconds rather than milliseconds.
_TIMEOUT_SECONDS = frozenset({"pi"})
_PATH_RE = re.compile(r'"(?:file_path|notebook_path|path)"\s*:\s*"([^"]+)"')
_COMMAND_RE = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)')
_TOKEN_SPLIT = re.compile(r"[\s;|&<>()'\"]+")
_PATHLIKE = re.compile(
    r"^(?:~|\.{1,2})?/[\w.@-]+(?:/[\w.@-]*)*$"   # absolute or ~/ ./ ../
    r"|^[\w.@-]+(?:/[\w.@-]*)+$"                  # relative with a slash
    r"|^[\w-]+\.[A-Za-z]{1,5}$")                  # bare filename with an extension
_SUBSTITUTION = re.compile(r"\$\([^)]*\)|`[^`]*`")
_ENV_ASSIGN = re.compile(r"^\w+=")
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec"})
_INTERRUPT_MARKERS = ("[Request interrupted by user",)

VERIFY_COMMANDS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"\bpytest\b", r"\bunittest\b", r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
    r"\bcargo\s+(?:test|check|clippy)\b", r"\bgo\s+(?:test|vet)\b", r"\bmake\b",
    r"\btsc\b", r"\bruff\b", r"\bty\s+check\b", r"\bvitest\b", r"\bmypy\b",
    r"\bshellcheck\b", r"\bbash\s+-n\b",
))


def role(harness: str, tool_name: str) -> str:
    """The role a tool plays, falling back to every known harness's table, then ``other``."""
    return _TOOL_ROLES.get(harness, {}).get(tool_name) or _ANY_HARNESS.get(tool_name, "other")


def call_args(call: ToolCall) -> dict[str, Any]:
    """The call's input as a dict; ``{}`` when the 2,000-char preview cut the JSON short."""
    try:
        parsed = json.loads(call.input_preview)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def command_of(call: ToolCall) -> str:
    """The shell command of an ``exec`` call, recovered even from a truncated preview."""
    value = call_args(call).get("command")
    if isinstance(value, str):
        return value
    match = _COMMAND_RE.search(call.input_preview)
    if match:
        try:
            return json.loads(f'"{match.group(1)}"')
        except (ValueError, UnicodeDecodeError):
            return match.group(1)
    # pi's bashExecution records (user `!` commands) keep the raw command, not JSON.
    return "" if call.input_preview.startswith('{"') else call.input_preview


def call_paths(harness: str, call: ToolCall) -> list[str]:
    """Paths a call touches: its path argument, or the path-like tokens of an exec command."""
    if role(harness, call.name) == "exec":
        return _command_paths(command_of(call))
    args = call_args(call)
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return [value]
    match = _PATH_RE.search(call.input_preview)
    return [match.group(1)] if match else []


def _command_paths(command: str) -> list[str]:
    paths = []
    for token in _TOKEN_SPLIT.split(command):
        if not token or token.startswith("-") or "://" in token:
            continue
        if _PATHLIKE.match(token):
            paths.append(token)
    return paths


def command_program(command: str) -> str:
    """The program a shell command runs, skipping ``cd x &&``, env assignments, ``$(...)``
    substitutions and wrappers such as ``sudo``."""
    cleaned = _SUBSTITUTION.sub("", command)
    for segment in re.split(r"&&|\|\||;|\||\r\n|\n", cleaned):
        words = segment.split()
        while words and (_ENV_ASSIGN.match(words[0]) or words[0] in _WRAPPERS):
            words.pop(0)
        if words and words[0] != "cd":
            return posixpath.basename(words[0])
    return ""


def run_key(harness: str, call: ToolCall) -> str:
    """What groups consecutive calls into one run: a directory, or an exec program."""
    call_role = role(harness, call.name)
    if call_role == "exec":
        return f"exec:{command_program(command_of(call))}"
    paths = call_paths(harness, call)
    if paths:
        return f"dir:{posixpath.dirname(paths[0].rstrip('/')) or paths[0]}"
    return f"{call_role}:{call.name}"


def declared_timeout_ms(harness: str, call: ToolCall) -> int | None:
    """The timeout the call itself declared, in milliseconds, or ``None``."""
    value = call_args(call).get("timeout")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value * 1000) if harness in _TIMEOUT_SECONDS else int(value)


def is_verify_command(command: str) -> bool:
    """Whether a shell command runs tests, a build, a type check or a linter."""
    return any(p.search(command) for p in VERIFY_COMMANDS)


def is_interrupt_marker(text: str) -> bool:
    """Whether a user message is Claude Code's synthetic "interrupted by user" marker."""
    return text.startswith(_INTERRUPT_MARKERS)
