"""Tiny builders for detector unit tests: a ParsedSession assembled field by field."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sessionkit.parse import FileOp, Message, ParsedSession, SysEvent, ToolCall

_BASE = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)

__all__ = ["FileOp", "SysEvent", "call", "msg", "session", "ts"]


def ts(minutes: float) -> str:
    """An ISO timestamp ``minutes`` after a fixed base time."""
    stamp = (_BASE + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def session(harness: str = "pi", sid: str = "s", **kw: Any) -> ParsedSession:
    return ParsedSession(sid=sid, source_id="src", path=f"/tmp/{sid}.jsonl", harness=harness,
                         **kw)


def call(line: int, name: str = "bash", args: dict[str, Any] | None = None, *,
         is_error: bool = False, err_class: str = "", at: str = "", result_at: str = "",
         dur_ms: int | None = None, digest: str = "", result_line: int = 0, out: str = "",
         tid: str = "") -> ToolCall:
    return ToolCall(tool_use_id=tid or f"t{line}-{name}", line=line, name=name, ts=at,
                    result_ts=result_at, result_line=result_line or line, dur_ms=dur_ms,
                    is_error=is_error, err_class=err_class, input_digest=digest or f"d{line}",
                    input_preview=json.dumps(args or {}), output_preview=out)


def msg(line: int, role: str = "assistant", *, text: str = "", at: str = "", stop: str = "",
        tool_calls: int = 0, reasoning_len: int = 0, error_text: str = "",
        markup_leak: str = "", repeat_ratio: float = 0.0, refs: list[str] | None = None,
        claims_done: bool = False, msg_id: str = "", model: str = "") -> Message:
    return Message(uuid=f"m{line}", line=line, ts=at, role=role, text_len=len(text),
                   preview=text, stop_reason=stop, tool_calls=tool_calls,
                   reasoning_len=reasoning_len, error_text=error_text,
                   markup_leak=markup_leak, repeat_ratio=repeat_ratio,
                   refs=list(refs or []), claims_done=claims_done, msg_id=msg_id,
                   model=model)
