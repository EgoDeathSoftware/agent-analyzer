"""Claude Code JSONL transcript → normalised event stream.

One pass per file produces a :class:`ParsedSession`: session-level totals plus flat lists of
messages, tool calls, file operations, system events and attachments. Everything downstream
(classification, reports) reads those lists rather than re-touching the JSONL.

Record shapes were verified against the live corpus rather than inferred:

* ``assistant`` — ``.message.content[]`` blocks (``text`` / ``thinking`` / ``tool_use``),
  ``.message.usage``, ``.message.model``, ``.message.stop_reason``
* ``user`` — ``.message.content`` is **either** a plain string **or** a block list containing
  ``tool_result`` blocks (``.is_error``, ``.content`` itself string-or-list)
* ``system`` — ``.subtype`` (``compact_boundary``, ``agents_killed``, ``turn_duration``,
  ``stop_hook_summary``, ``local_command``, ``away_summary``)
* ``attachment`` — ``.attachment.type`` (``hook_success``, ``command_permissions``,
  ``read_truncation_notice``, ``total_tokens_reminder``, …)
* ``ai-title`` / ``last-prompt`` / ``mode`` — sidecar records carrying session metadata
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sessionkit import pricing
from sessionkit.redact import preview

MSG_PREVIEW = 200
#: Generous on purpose: this is the only copy of a tool's input/output kept in memory, and
#: ``sk commands``' whole purpose is showing what actually ran. A tighter cap here forced a
#: second, unmarked truncation downstream in the renderer — see render.MAX_CELL_CHARS for the
#: display-time cap, which *does* mark what it cuts.
INPUT_PREVIEW = 2000
OUTPUT_PREVIEW = 2000

#: Tool name → file operation. Mirrors the tracker's own mapping.
FILE_TOOLS = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
}
#: Input keys that carry the target path, in priority order.
PATH_KEYS = ("file_path", "notebook_path", "path")


@dataclass
class ToolCall:
    """One tool invocation, paired with its result when the result arrived."""

    tool_use_id: str
    line: int
    name: str
    ts: str
    result_ts: str = ""
    dur_ms: int | None = None
    is_error: bool = False
    err_class: str = ""
    err_detail: str = ""
    input_digest: str = ""
    input_preview: str = ""
    output_preview: str = ""
    out_bytes: int = 0


@dataclass
class Message:
    """One user or assistant turn."""

    uuid: str
    line: int
    ts: str
    role: str
    model: str = ""
    tok_in: int = 0
    tok_out: int = 0
    tok_cr: int = 0
    tok_cc: int = 0
    text_len: int = 0
    preview: str = ""


@dataclass
class FileOp:
    """A read/write/edit against a path."""

    path: str
    op: str
    ts: str
    tool_use_id: str


@dataclass
class SysEvent:
    """A ``type: system`` record, keyed by its subtype."""

    line: int
    ts: str
    subtype: str
    detail: str = ""


@dataclass
class Attach:
    """A ``type: attachment`` record, keyed by its attachment type."""

    line: int
    ts: str
    atype: str
    detail: str = ""


@dataclass
class ParsedSession:
    """Everything one transcript file yields."""

    sid: str
    source_id: str
    path: str
    cwd: str = ""
    git_branch: str = ""
    version: str = ""
    title: str = ""
    first_prompt: str = ""
    started_at: str = ""
    ended_at: str = ""
    turns: int = 0
    model: str = ""
    is_subagent: bool = False
    parent_sid: str = ""
    agent_type: str = ""
    cost_usd: float = 0.0
    tok_in: int = 0
    tok_out: int = 0
    tok_cache_read: int = 0
    tok_cache_create: int = 0
    malformed_lines: int = 0
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    files: list[FileOp] = field(default_factory=list)
    sysev: list[SysEvent] = field(default_factory=list)
    attach: list[Attach] = field(default_factory=list)
    end_state: str = "unknown"
    end_reason: str = ""
    tail_signal: str = ""


def basename_of(path: str) -> str:
    """Return the final path component, tolerating both POSIX and Windows separators."""
    cleaned = (path or "").replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def project_key(cwd: str, source_id: str, dir_name: str) -> str:
    """Derive the cross-source project identity for a session.

    Mirrors ``deriveProjectKey`` in ``server/src/project-key.ts``: the lowercased basename of
    ``cwd`` so the same folder merges across machines, falling back to a source-scoped key when
    no cwd was recorded.
    """
    base = basename_of(cwd)
    return base.lower() if base else f"{source_id}:{dir_name}"


def block_text(content: Any) -> str:
    """Flatten a content field into plain text.

    Handles the three shapes seen in transcripts: a bare string, a list of blocks, or a nested
    list inside a ``tool_result``.
    """
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


def read_line(path: str | Path, line_no: int) -> dict[str, Any] | None:
    """Re-read one line of a transcript by its 1-indexed line number.

    ``tail_context`` needs this because completion markers routinely land past the
    ``MSG_PREVIEW`` cap already baked into ``Message.preview``. Returns ``None`` if the
    file is unreadable, the line doesn't exist, or it isn't valid JSON — callers fall back
    to the capped preview in that case.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for current, raw in enumerate(handle, start=1):
                if current == line_no:
                    try:
                        rec = json.loads(raw.strip())
                    except json.JSONDecodeError:
                        return None
                    return rec if isinstance(rec, dict) else None
    except OSError:
        return None
    return None


def digest(value: Any) -> str:
    """Stable short hash of a tool input, used to detect repeated identical calls."""
    try:
        canonical = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(value)
    return hashlib.sha1(canonical.encode("utf-8", "replace")).hexdigest()[:16]


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


class _Parser:
    """Accumulates one file's records into a :class:`ParsedSession`."""

    def __init__(self, path: Path, source_id: str, sid: str) -> None:
        self.out = ParsedSession(sid=sid, source_id=source_id, path=str(path))
        self.pending: dict[str, ToolCall] = {}

    def feed(self, line_no: int, rec: dict[str, Any]) -> None:
        """Dispatch one decoded record by its ``type``."""
        kind = rec.get("type")
        self._meta(rec)
        if kind == "assistant":
            self._assistant(line_no, rec)
        elif kind == "user":
            self._user(line_no, rec)
        elif kind == "system":
            self._system(line_no, rec)
        elif kind == "attachment":
            self._attachment(line_no, rec)
        elif kind == "progress":
            self._progress(line_no, rec)
        elif kind == "ai-title":
            self.out.title = str(rec.get("aiTitle") or self.out.title)
        elif kind == "last-prompt" and not self.out.first_prompt:
            self.out.first_prompt = preview(str(rec.get("lastPrompt") or ""), MSG_PREVIEW)

    def _meta(self, rec: dict[str, Any]) -> None:
        """Pick up session-level fields that appear on any record type."""
        out = self.out
        out.cwd = str(rec.get("cwd") or out.cwd)
        out.git_branch = str(rec.get("gitBranch") or out.git_branch)
        out.version = str(rec.get("version") or out.version)
        if rec.get("isSidechain") or rec.get("agentId"):
            out.is_subagent = True
            # A subagent transcript records its *parent's* sessionId. Keying on that would
            # collapse every subagent into its parent — 36 of 86 files, in this corpus.
            out.parent_sid = str(rec.get("sessionId") or out.parent_sid)
        ts = str(rec.get("timestamp") or "")
        if ts:
            if not out.started_at or ts < out.started_at:
                out.started_at = ts
            if ts > out.ended_at:
                out.ended_at = ts

    def _assistant(self, line_no: int, rec: dict[str, Any]) -> None:
        """Record an assistant turn: usage, text preview, and any tool_use blocks."""
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        ts = str(rec.get("timestamp") or "")
        model = str(msg.get("model") or "")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        tok_in = int(usage.get("input_tokens") or 0)
        tok_out = int(usage.get("output_tokens") or 0)
        tok_cr = int(usage.get("cache_read_input_tokens") or 0)
        tok_cc = int(usage.get("cache_creation_input_tokens") or 0)

        out = self.out
        out.tok_in += tok_in
        out.tok_out += tok_out
        out.tok_cache_read += tok_cr
        out.tok_cache_create += tok_cc
        out.cost_usd += pricing.cost(model, tok_in, tok_out, tok_cr, tok_cc)
        if model:
            out.model = model

        text = block_text(msg.get("content"))
        out.messages.append(Message(
            uuid=str(rec.get("uuid") or ""), line=line_no, ts=ts, role="assistant",
            model=model, tok_in=tok_in, tok_out=tok_out, tok_cr=tok_cr, tok_cc=tok_cc,
            text_len=len(text), preview=preview(text, MSG_PREVIEW),
        ))
        self._tool_uses(line_no, ts, msg.get("content"))

    def _tool_uses(self, line_no: int, ts: str, content: Any) -> None:
        """Register every ``tool_use`` block in an assistant turn as a pending call."""
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            raw_input = block.get("input")
            call = ToolCall(
                tool_use_id=str(block.get("id") or ""), line=line_no,
                name=str(block.get("name") or "?"), ts=ts,
                input_digest=digest(raw_input),
                input_preview=preview(json.dumps(raw_input, default=str), INPUT_PREVIEW),
            )
            self.out.tools.append(call)
            if call.tool_use_id:
                self.pending[call.tool_use_id] = call
            self._file_op(call, raw_input, ts)

    def _file_op(self, call: ToolCall, raw_input: Any, ts: str) -> None:
        """Record a file operation when the tool is one of the file tools."""
        op = FILE_TOOLS.get(call.name)
        if not op or not isinstance(raw_input, dict):
            return
        for key in PATH_KEYS:
            value = raw_input.get(key)
            if isinstance(value, str) and value:
                self.out.files.append(FileOp(value, op, ts, call.tool_use_id))
                return

    def _user(self, line_no: int, rec: dict[str, Any]) -> None:
        """Record a user turn, or attach tool results to their pending calls."""
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        ts = str(rec.get("timestamp") or "")
        content = msg.get("content")
        results = self._tool_results(ts, content)
        if results:
            return  # a results-only turn is transport, not a prompt

        text = block_text(content)
        if not rec.get("isMeta") and text.strip():
            self.out.turns += 1
            if not self.out.first_prompt:
                self.out.first_prompt = preview(text, MSG_PREVIEW)
        self.out.messages.append(Message(
            uuid=str(rec.get("uuid") or ""), line=line_no, ts=ts, role="user",
            text_len=len(text), preview=preview(text, MSG_PREVIEW),
        ))

    def _tool_results(self, ts: str, content: Any) -> int:
        """Pair ``tool_result`` blocks with their pending calls. Returns how many were found."""
        if not isinstance(content, list):
            return 0
        found = 0
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            found += 1
            call = self.pending.pop(str(block.get("tool_use_id") or ""), None)
            if call is None:
                continue
            body = block_text(block.get("content"))
            call.result_ts = ts
            call.dur_ms = _delta_ms(call.ts, ts)
            call.is_error = bool(block.get("is_error"))
            call.out_bytes = len(body)
            call.output_preview = preview(body, OUTPUT_PREVIEW)
        return found

    def _system(self, line_no: int, rec: dict[str, Any]) -> None:
        """Record a system event, keeping the fields each subtype carries."""
        subtype = str(rec.get("subtype") or "")
        detail = ""
        if subtype == "turn_duration":
            detail = str(rec.get("durationMs") or "")
        elif subtype == "compact_boundary":
            meta = rec.get("compactMetadata")
            if isinstance(meta, dict):
                detail = f"{meta.get('trigger', '?')}:{meta.get('preTokens', 0)}"
        else:
            detail = preview(str(rec.get("content") or ""), 120)
        self.out.sysev.append(SysEvent(line_no, str(rec.get("timestamp") or ""), subtype, detail))

    def _attachment(self, line_no: int, rec: dict[str, Any]) -> None:
        """Record an attachment event, skipping the high-volume token reminders."""
        att = rec.get("attachment")
        if not isinstance(att, dict):
            return
        atype = str(att.get("type") or "")
        if atype == "total_tokens_reminder":
            return  # ~862 of these in the corpus; they carry no analytic signal
        self.out.attach.append(Attach(line_no, str(rec.get("timestamp") or ""), atype))

    def _progress(self, line_no: int, rec: dict[str, Any]) -> None:
        """Record a ``progress`` record — subagent transcripts use these for hook events."""
        data = rec.get("data")
        if not isinstance(data, dict):
            return
        subtype = str(data.get("type") or "progress")
        detail = str(data.get("hookName") or data.get("hookEvent") or "")
        self.out.sysev.append(
            SysEvent(line_no, str(rec.get("timestamp") or ""), subtype, preview(detail, 120))
        )

    def finish(self) -> ParsedSession:
        """Return the accumulated session, flagging any unresolved tool calls."""
        for call in self.pending.values():
            call.err_class = "no-result"
        return self.out


def parse_file(path: Path, source_id: str) -> ParsedSession:
    """Parse one transcript file into a normalised session.

    Malformed lines are counted rather than fatal — a partially-written transcript for a live
    session is normal, and dropping the whole file would silently hide an active session.

    Args:
        path: Path to the ``.jsonl`` transcript.
        source_id: Owning source identifier.

    Returns:
        The parsed session; ``sid`` falls back to the filename stem when no record carried one.
    """
    parser = _Parser(path, source_id, path.stem)
    sid_seen = ""
    agent_seen = ""
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
            sid_seen = sid_seen or str(rec.get("sessionId") or "")
            agent_seen = agent_seen or str(rec.get("agentId") or "")
            parser.feed(line_no, rec)
    out = parser.finish()
    # A subagent is identified by its agentId; its sessionId belongs to the parent.
    out.sid = agent_seen or sid_seen or out.sid
    if out.is_subagent:
        out.agent_type = _agent_type(path)
    return out


def _agent_type(path: Path) -> str:
    """Read the ``agentType`` from a subagent transcript's ``.meta.json`` sidecar."""
    meta = path.with_suffix(".meta.json")
    if not meta.is_file():
        return ""
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("agentType") or "") if isinstance(data, dict) else ""
