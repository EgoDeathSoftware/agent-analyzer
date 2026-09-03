"""Reads ``settings.json`` and ``.claude.json`` — single-file, read-only config lookups.

These are joined against tool-call failures in ``query.py`` (``sk hooks``), which is why they
live in a module rather than being read ad hoc: the join is cross-record and belongs in the CLI
per PLAN.md §4, even though any one read here is a single small file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Every hook command in this corpus signals its rejection with `echo 'BLOCKED: ...'` (or
#: double-quoted) to stderr before exiting non-zero. Extracting that literal string is what lets
#: a failure be attributed to the specific hook that emitted it, not just to its matcher.
_ECHO_MSG = re.compile(r"echo\s+['\"]([^'\"]+)['\"]")

_DENY_RULE = re.compile(r"^(\w+)\((.*)\)$")


@dataclass
class HookDef:
    """One ``hooks.<event>[].hooks[]`` entry from ``settings.json``."""

    event: str
    matcher: str
    command: str
    messages: list[str] = field(default_factory=list)


def read_settings(root: Path) -> dict[str, Any]:
    """Read ``<root>/settings.json``. Missing or malformed returns an empty mapping."""
    try:
        data = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def hook_defs(settings: dict[str, Any]) -> list[HookDef]:
    """Flatten ``settings.json``'s ``hooks`` block into one entry per command script."""
    out: list[HookDef] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher") or "")
            inner = entry.get("hooks")
            for h in inner if isinstance(inner, list) else []:
                if not isinstance(h, dict):
                    continue
                command = str(h.get("command") or "")
                out.append(HookDef(event=str(event), matcher=matcher, command=command,
                                   messages=_ECHO_MSG.findall(command)))
    return out


def deny_rules(settings: dict[str, Any]) -> list[tuple[str, str]]:
    """``permissions.deny`` entries, parsed into ``(tool, pattern)`` pairs.

    Entries that don't match the ``Tool(pattern)`` shape are skipped rather than guessed at.
    """
    perms = settings.get("permissions")
    deny = perms.get("deny") if isinstance(perms, dict) else None
    if not isinstance(deny, list):
        return []
    out = []
    for rule in deny:
        match = _DENY_RULE.match(str(rule).strip())
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def cleanup_period_days(settings: dict[str, Any]) -> int | None:
    """The effective retention window, or ``None`` if unset."""
    value = settings.get("cleanupPeriodDays")
    return int(value) if isinstance(value, (int, float)) else None


def read_allowed_tools(claude_json_path: Path, cwd: str) -> list[str]:
    """``<home>/.claude.json:projects[cwd].allowedTools``, tolerating absence of any part."""
    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    project = projects.get(cwd) if isinstance(projects, dict) else None
    allowed = project.get("allowedTools") if isinstance(project, dict) else None
    return [str(a) for a in allowed] if isinstance(allowed, list) else []


def read_last_model_usage(claude_json_path: Path, cwd: str) -> dict[str, dict[str, Any]]:
    """``<home>/.claude.json:projects[cwd].lastModelUsage`` — per-model tokens and cost for
    that project's most recent session, computed by Claude Code itself rather than either
    hand-maintained pricing table (PLAN.md §3.2.2). Tolerates absence of any part.
    """
    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    project = projects.get(cwd) if isinstance(projects, dict) else None
    usage = project.get("lastModelUsage") if isinstance(project, dict) else None
    return usage if isinstance(usage, dict) else {}


def read_last_session_id(claude_json_path: Path, cwd: str) -> str:
    """``<home>/.claude.json:projects[cwd].lastSessionId`` — which session
    ``lastModelUsage`` describes, since the field is overwritten per project and only ever
    covers the single most recent session (PLAN.md §3.2.2)."""
    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    projects = data.get("projects") if isinstance(data, dict) else None
    project = projects.get(cwd) if isinstance(projects, dict) else None
    return str(project.get("lastSessionId") or "") if isinstance(project, dict) else ""
