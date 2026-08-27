"""Builders for synthetic transcripts with known, hand-placed defects.

Fixtures are constructed rather than committed so each test states the exact shape it depends
on: a test that asserts loop detection builds the loop inline, and a reviewer can see the
input and the expectation together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SID = "11111111-2222-3333-4444-555555555555"
CWD = "/home/dev/myproject"


def user(text: str, ts: str = "2026-08-01T00:00:00Z", **extra: Any) -> dict[str, Any]:
    """A plain user turn."""
    return {"type": "user", "sessionId": SID, "cwd": CWD, "timestamp": ts,
            "uuid": f"u-{ts}", "message": {"role": "user", "content": text}, **extra}


def assistant(blocks: list[dict[str, Any]], ts: str = "2026-08-01T00:00:01Z",
              model: str = "claude-opus-5", usage: dict[str, int] | None = None,
              **extra: Any) -> dict[str, Any]:
    """An assistant turn carrying arbitrary content blocks."""
    return {"type": "assistant", "sessionId": SID, "cwd": CWD, "timestamp": ts,
            "uuid": f"a-{ts}",
            "message": {"role": "assistant", "model": model, "content": blocks,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 5}}, **extra}


def tool_use(tid: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """A ``tool_use`` content block."""
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def tool_result(tid: str, body: str, is_error: bool = False,
                ts: str = "2026-08-01T00:00:02Z") -> dict[str, Any]:
    """A user turn carrying one ``tool_result`` block."""
    return {"type": "user", "sessionId": SID, "cwd": CWD, "timestamp": ts, "uuid": f"r-{tid}",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": body,
                 "is_error": is_error}]}}


def system(subtype: str, ts: str = "2026-08-01T00:00:03Z", **extra: Any) -> dict[str, Any]:
    """A ``type: system`` record."""
    return {"type": "system", "sessionId": SID, "cwd": CWD, "timestamp": ts,
            "uuid": f"s-{subtype}-{ts}", "subtype": subtype, **extra}


def write(tmp: Path, records: list[dict[str, Any]], name: str = f"{SID}.jsonl") -> Path:
    """Write records as JSONL into a project directory and return the file path."""
    project = tmp / "projects" / "-home-dev-myproject"
    project.mkdir(parents=True, exist_ok=True)
    path = project / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def write_subagent(tmp: Path, records: list[dict[str, Any]], agent_id: str = "abc123",
                   agent_type: str = "Explore") -> Path:
    """Write a subagent transcript at the nested path Claude Code actually uses."""
    folder = tmp / "projects" / "-home-dev-myproject" / SID / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"agent-{agent_id}.jsonl"
    tagged = [{**r, "isSidechain": True, "agentId": agent_id} for r in records]
    path.write_text("\n".join(json.dumps(r) for r in tagged) + "\n", encoding="utf-8")
    path.with_suffix(".meta.json").write_text(json.dumps({"agentType": agent_type}),
                                              encoding="utf-8")
    return path


def simple_session() -> list[dict[str, Any]]:
    """A well-formed session: one prompt, one successful tool call, one reply."""
    return [
        user("add a feature"),
        assistant([tool_use("t1", "Read", {"file_path": "/home/dev/myproject/a.py"})]),
        tool_result("t1", "file contents"),
        assistant([{"type": "text", "text": "done"}], ts="2026-08-01T00:00:04Z"),
    ]
