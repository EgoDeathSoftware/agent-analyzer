"""pi (@earendil-works/pi-coding-agent) JSONL session -> normalised event stream.

One pass per file produces a :class:`~sessionkit.parse.ParsedSession`, reusing the same
dataclasses `sessionkit.parse` builds for Claude Code transcripts so every downstream consumer
(classify, query, render) needs no pi-specific code.

Record shapes are taken from the package's own documentation
(`node_modules/@earendil-works/pi-coding-agent/docs/session-format.md`, version 0.84.1),
verified against the package's shipped `.d.ts` files — not from a live corpus. See
docs/superpowers/specs/2026-09-25-pi-analyzer-design.md Sec 1 for why, and Task 7 of the plan
that implements this for the real-session validation step this gap requires.

Entries are consumed in file order (a tree's flattened write order), not walked from the active
leaf backward — see the spec Sec 3.4 for why, and what a leaf-only mode would need.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sessionkit.parse import FileOp, Message, ParsedSession, SysEvent, ToolCall, digest
from sessionkit.redact import preview

MSG_PREVIEW = 200
INPUT_PREVIEW = 2000
OUTPUT_PREVIEW = 2000

#: pi's builtin file tools use lowercase names and a single `path` argument key — unlike
#: Claude Code's capitalised `Read`/`Edit`/`MultiEdit`/`NotebookEdit` with `file_path`.
FILE_TOOLS = {"read": "read", "write": "write", "edit": "edit"}
PATH_KEY = "path"


def _text_of(content: Any) -> str:
    """Flatten a `string | (TextContent|ImageContent|ThinkingContent)[]` field to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            parts.append(str(block.get("text") or block.get("thinking") or ""))
    return "\n".join(p for p in parts if p)


def _delta_ms(start: str, end: str) -> int | None:
    """Milliseconds between two ISO-8601 timestamps, or ``None`` if either is unusable."""
    from datetime import datetime

    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((b - a).total_seconds() * 1000)


def _ms_to_iso(ms: Any) -> str:
    """Convert a pi-ai `Message.timestamp` (Unix ms) to the ISO string every other `ts` field
    in this module uses. Nested subagent messages carry this instead of an entry wrapper."""
    from datetime import datetime, timezone

    if not isinstance(ms, (int, float)):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


class _Parser:
    """Accumulates one pi session file's entries into a ParsedSession."""

    def __init__(self, path: Path, source_id: str, sid: str,
                session: ParsedSession | None = None) -> None:
        self.out = session if session is not None else ParsedSession(
            sid=sid, source_id=source_id, path=str(path))
        self.pending: dict[str, ToolCall] = {}

    def _touch_ts(self, ts: str) -> None:
        out = self.out
        if not ts:
            return
        if not out.started_at or ts < out.started_at:
            out.started_at = ts
        if ts > out.ended_at:
            out.ended_at = ts

    def feed(self, line_no: int, rec: dict[str, Any]) -> None:
        """Dispatch one decoded top-level entry by its ``type``."""
        etype = rec.get("type")
        if etype == "session":
            self._header(rec)
        elif etype == "message":
            self._message(line_no, rec)
        elif etype in ("model_change", "thinking_level_change", "label", "session_info"):
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(SysEvent(line_no, ts, etype))
        elif etype == "compaction":
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(
                SysEvent(line_no, ts, "compaction", str(rec.get("tokensBefore") or "")))
        elif etype == "branch_summary":
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            self.out.sysev.append(SysEvent(line_no, ts, "branch_summary"))
        elif etype in ("custom", "custom_message"):
            ts = str(rec.get("timestamp") or "")
            self._touch_ts(ts)
            custom_type = str(rec.get("customType") or "")
            self.out.sysev.append(SysEvent(line_no, ts, f"custom:{custom_type}"))
        else:
            self.out.malformed_lines += 1

    def _header(self, rec: dict[str, Any]) -> None:
        out = self.out
        out.cwd = str(rec.get("cwd") or out.cwd)
        self._touch_ts(str(rec.get("timestamp") or ""))

    def _message(self, line_no: int, rec: dict[str, Any]) -> None:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            self.out.malformed_lines += 1
            return
        role = msg.get("role")
        ts = str(rec.get("timestamp") or "")
        self._touch_ts(ts)
        if role == "user":
            self._user(line_no, ts, msg)
        elif role == "assistant":
            self._assistant(line_no, ts, msg)
        elif role == "toolResult":
            self._tool_result(line_no, ts, msg)
        elif role == "bashExecution":
            self._bash(line_no, ts, msg)
        # custom / branchSummary / compactionSummary message roles carry no reportable field
        # beyond what their own custom_message/branch_summary/compaction entry already recorded.

    def _user(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        text = _text_of(msg.get("content"))
        out = self.out
        if text.strip():
            out.turns += 1
            if not out.first_prompt:
                out.first_prompt = preview(text, MSG_PREVIEW)
        out.messages.append(Message(uuid=f"u-{line_no}", line=line_no, ts=ts, role="user",
                                    text_len=len(text), preview=preview(text, MSG_PREVIEW)))

    def _assistant(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        out = self.out
        model = str(msg.get("model") or "")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        tok_in = int(usage.get("input") or 0)
        tok_out = int(usage.get("output") or 0)
        tok_cr = int(usage.get("cacheRead") or 0)
        tok_cc = int(usage.get("cacheWrite") or 0)
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        out.tok_in += tok_in
        out.tok_out += tok_out
        out.tok_cache_read += tok_cr
        out.tok_cache_create += tok_cc
        out.cost_usd += float(cost.get("total") or 0.0)
        if model:
            out.model = model

        content = msg.get("content")
        text = _text_of(content)
        out.messages.append(Message(uuid=f"a-{line_no}", line=line_no, ts=ts, role="assistant",
                                    model=model, tok_in=tok_in, tok_out=tok_out, tok_cr=tok_cr,
                                    tok_cc=tok_cc, text_len=len(text),
                                    preview=preview(text, MSG_PREVIEW)))
        if msg.get("stopReason") == "error":
            out.sysev.append(SysEvent(line_no, ts, "api_error",
                                      preview(str(msg.get("errorMessage") or ""), 120)))
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._open_tool_call(line_no, ts, block)

    def _open_tool_call(self, line_no: int, ts: str, block: dict[str, Any]) -> None:
        raw_input = block.get("arguments")
        call = ToolCall(tool_use_id=str(block.get("id") or ""), line=line_no,
                        name=str(block.get("name") or "?"), ts=ts,
                        input_digest=digest(raw_input),
                        input_preview=preview(json.dumps(raw_input, default=str), INPUT_PREVIEW))
        self.out.tools.append(call)
        if call.tool_use_id:
            self.pending[call.tool_use_id] = call
        self._file_op(call, raw_input, ts)

    def _file_op(self, call: ToolCall, raw_input: Any, ts: str) -> None:
        op = FILE_TOOLS.get(call.name)
        if not op or not isinstance(raw_input, dict):
            return
        path = raw_input.get(PATH_KEY)
        if isinstance(path, str) and path:
            self.out.files.append(FileOp(path, op, ts, call.tool_use_id, call.line))

    def _tool_result(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        call = self.pending.pop(str(msg.get("toolCallId") or ""), None)
        if call is None:
            return
        body = _text_of(msg.get("content"))
        call.result_ts = ts
        call.result_line = line_no
        call.dur_ms = _delta_ms(call.ts, ts)
        call.is_error = bool(msg.get("isError"))
        call.out_bytes = len(body)
        call.output_preview = preview(body, OUTPUT_PREVIEW)
        if call.name == "subagent":
            details = msg.get("details")
            children = _subagent_children(
                self.out.sid, self.out.source_id, self.out.path, self.out.cwd,
                call, details if isinstance(details, dict) else {})
            self.out.child_sessions.extend(children)
            self.out.dispatch_edges.extend((call.tool_use_id, c.sid) for c in children)

    def _bash(self, line_no: int, ts: str, msg: dict[str, Any]) -> None:
        exit_code = msg.get("exitCode")
        cancelled = bool(msg.get("cancelled"))
        is_error = cancelled or (exit_code is not None and exit_code != 0)
        output = str(msg.get("output") or "")
        call = ToolCall(tool_use_id=f"bash-{line_no}", line=line_no, name="bash", ts=ts,
                        result_ts=ts, result_line=line_no, is_error=is_error,
                        out_bytes=len(output), output_preview=preview(output, OUTPUT_PREVIEW),
                        input_preview=preview(str(msg.get("command") or ""), INPUT_PREVIEW),
                        input_digest=digest(msg.get("command")))
        self.out.tools.append(call)

    def finish(self) -> ParsedSession:
        for call in self.pending.values():
            call.err_class = "no-result"
        return self.out


def _subagent_children(parent_sid: str, source_id: str, path: str, parent_cwd: str,
                       call: ToolCall, details: dict[str, Any]) -> list[ParsedSession]:
    """Expand one closed `subagent` tool call's nested `details.results[]` into synthetic
    child ParsedSessions (spec Sec 4).

    Returns ``[]`` if `details` carries no results — covers both a non-subagent tool that
    happens to be named "subagent" in some other extension and a subagent call that failed
    before producing any result.
    """
    results = details.get("results") if isinstance(details, dict) else None
    if not isinstance(results, list) or not results:
        return []

    children: list[ParsedSession] = []
    for i, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        child_sid = f"{parent_sid}:{call.tool_use_id}:{i}"
        child = ParsedSession(sid=child_sid, source_id=source_id, path=path, cwd=parent_cwd,
                              is_subagent=True, parent_sid=parent_sid,
                              agent_type=str(result.get("agent") or ""))
        sub = _Parser(Path(path), source_id, child_sid, session=child)
        for msg in result.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            ts = _ms_to_iso(msg.get("timestamp"))
            if role == "user":
                sub._user(0, ts, msg)
            elif role == "assistant":
                sub._assistant(0, ts, msg)
            elif role == "toolResult":
                sub._tool_result(0, ts, msg)
            elif role == "bashExecution":
                sub._bash(0, ts, msg)
        sub.finish()

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        child.tok_in = int(usage.get("input") or 0)
        child.tok_out = int(usage.get("output") or 0)
        child.tok_cache_read = int(usage.get("cacheRead") or 0)
        child.tok_cache_create = int(usage.get("cacheWrite") or 0)
        child.cost_usd = float(usage.get("cost") or 0.0)

        exit_code = result.get("exitCode")
        stop_reason = result.get("stopReason")
        if exit_code == 0 and stop_reason not in ("error", "aborted"):
            child.end_state, child.end_reason = "complete", ""
        else:
            child.end_state = "error-cascade"
            child.end_reason = str(result.get("errorMessage") or result.get("stderr") or "")

        children.append(child)
    return children


def parse_file(path: Path, source_id: str) -> ParsedSession:
    """Parse one pi session JSONL file into a normalised session.

    Malformed or unrecognised lines are counted rather than fatal, matching
    :func:`sessionkit.parse.parse_file`'s behaviour for the same reason: a partially written
    session for a live ``pi`` run is normal, not corrupt.

    Args:
        path: Path to the session ``.jsonl`` file.
        source_id: Owning source identifier.

    Returns:
        The parsed session; ``sid`` falls back to the filename stem if no header line carried
        an ``id``.
    """
    parser = _Parser(path, source_id, path.stem)
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return parser.finish()
    with handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                parser.out.malformed_lines += 1
                continue
            if not isinstance(rec, dict):
                parser.out.malformed_lines += 1
                continue
            if line_no == 1 and rec.get("type") == "session" and rec.get("id"):
                parser.out.sid = str(rec["id"])
            parser.feed(line_no, rec)
    return parser.finish()
