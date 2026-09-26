"""Builders for synthetic pi (@earendil-works/pi-coding-agent) session JSONL.

Mirrors tests/fixtures.py's role for Claude Code transcripts: fixtures are constructed inline
so each test states the exact shape it depends on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SID = "aaaa1111-bbbb-2222-cccc-3333dddd4444"
CWD = "/home/dev/myproject"


def header(ts: str = "2026-09-01T00:00:00.000Z", sid: str = SID, **extra: Any) -> dict[str, Any]:
    """The first line of a pi session file: metadata only, no id/parentId."""
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": CWD, **extra}


def _entry(etype: str, entry_id: str, parent_id: str | None, ts: str,
          **extra: Any) -> dict[str, Any]:
    return {"type": etype, "id": entry_id, "parentId": parent_id, "timestamp": ts, **extra}


def user_msg(entry_id: str, parent_id: str | None, text: str,
            ts: str = "2026-09-01T00:00:01.000Z") -> dict[str, Any]:
    return _entry("message", entry_id, parent_id, ts,
                 message={"role": "user", "content": text})


def assistant_msg(entry_id: str, parent_id: str | None, content: list[dict[str, Any]],
                  ts: str = "2026-09-01T00:00:02.000Z", model: str = "claude-sonnet-5",
                  usage: dict[str, Any] | None = None, stop_reason: str = "stop",
                  error_message: str = "") -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant", "content": content, "provider": "anthropic", "model": model,
        "usage": usage or {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0,
                           "totalTokens": 150,
                           "cost": {"input": 0.001, "output": 0.001, "cacheRead": 0.0,
                                    "cacheWrite": 0.0, "total": 0.002}},
        "stopReason": stop_reason,
    }
    if error_message:
        msg["errorMessage"] = error_message
    return _entry("message", entry_id, parent_id, ts, message=msg)


def api_error_session() -> list[dict[str, Any]]:
    """header -> user -> assistant that failed before producing content.

    Shape copied from a real pi 0.84 session whose provider auth failed: empty content,
    zero usage, ``stopReason: "error"`` and the provider's message in ``errorMessage``.
    """
    zero_usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}
    return [
        header(),
        user_msg("e1", None, "list the files"),
        assistant_msg("e2", "e1", [], usage=zero_usage, stop_reason="error",
                      error_message="OAuth refresh failed for anthropic: Refresh token expired"),
    ]


def tool_call_block(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def tool_result_msg(entry_id: str, parent_id: str | None, call_id: str, tool_name: str,
                    text: str, is_error: bool = False, details: dict[str, Any] | None = None,
                    ts: str = "2026-09-01T00:00:03.000Z") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "toolResult", "toolCallId": call_id, "toolName": tool_name,
                           "content": [{"type": "text", "text": text}], "isError": is_error}
    if details is not None:
        msg["details"] = details
    return _entry("message", entry_id, parent_id, ts, message=msg)


def bash_msg(entry_id: str, parent_id: str | None, command: str, output: str,
            exit_code: int | None = 0, cancelled: bool = False,
            ts: str = "2026-09-01T00:00:03.000Z") -> dict[str, Any]:
    return _entry("message", entry_id, parent_id, ts, message={
        "role": "bashExecution", "command": command, "output": output, "exitCode": exit_code,
        "cancelled": cancelled, "truncated": False,
    })


def compaction(entry_id: str, parent_id: str | None, summary: str, tokens_before: int,
              ts: str = "2026-09-01T00:00:04.000Z") -> dict[str, Any]:
    return _entry("compaction", entry_id, parent_id, ts, summary=summary,
                 tokensBefore=tokens_before)


def write(tmp: Path, entries: list[dict[str, Any]],
         name: str = f"20260901T000000_{SID}.jsonl") -> Path:
    """Write entries as JSONL under pi's own layout: sessions/--<encoded-cwd>--/<file>."""
    folder = tmp / "sessions" / "--home-dev-myproject--"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def subagent_result(agent: str, task: str, output_text: str, exit_code: int = 0,
                    stop_reason: str = "stop", error_message: str = "",
                    usage_cost: float = 0.01) -> dict[str, Any]:
    """One entry of a `subagent` toolResult's `details.results[]` array."""
    return {
        "agent": agent, "agentSource": "user", "task": task, "exitCode": exit_code,
        "stopReason": stop_reason, "errorMessage": error_message,
        "usage": {"input": 200, "output": 100, "cacheRead": 0, "cacheWrite": 0,
                 "cost": usage_cost},
        "messages": [
            {"role": "user", "content": task, "timestamp": 1735689600000},
            {"role": "assistant", "content": [{"type": "text", "text": output_text}],
             "provider": "anthropic", "model": "claude-haiku-4-5", "stopReason": stop_reason,
             "timestamp": 1735689601000},
        ],
    }


def simple_session() -> list[dict[str, Any]]:
    """header -> user -> assistant(toolCall) -> toolResult -> assistant(text)."""
    return [
        header(),
        user_msg("e1", None, "read a file"),
        assistant_msg("e2", "e1", [tool_call_block("c1", "read", {"path": "/a.py"})]),
        tool_result_msg("e3", "e2", "c1", "read", "file contents"),
        assistant_msg("e4", "e3", [{"type": "text", "text": "done"}]),
    ]
