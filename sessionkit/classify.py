"""Session end-state derivation, tail signals, and anomaly detection.

The failure taxonomy (error classes, hints, thresholds) lives in ``sessionkit.taxonomy``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from sessionkit.parse import ParsedSession, SysEvent, ToolCall
from sessionkit.taxonomy import THRESHOLDS, classify_error, signature



@dataclass
class Anomaly:
    """One detected problem in a session."""

    kind: str
    detail: str
    count: int
    lines: list[int]




def annotate_errors(session: ParsedSession) -> None:
    """Fill in ``err_class``/``err_detail`` on every failed tool call, in place."""
    for call in session.tools:
        if call.err_class == "no-result":
            call.err_detail = "tool_use with no matching result"
            continue
        if not call.is_error:
            continue
        cls, _hint = classify_error(call.output_preview)
        call.err_class = cls
        call.err_detail = signature(call.output_preview)


def _final_api_error(session: ParsedSession) -> SysEvent | None:
    """The ``api_error`` event raised by the session's last message, if that turn failed."""
    last_line = session.messages[-1].line
    for event in reversed(session.sysev):
        if event.subtype == "api_error" and event.line == last_line:
            return event
    return None


def derive_end_state(session: ParsedSession) -> tuple[str, str]:
    """Decide how a session ended, and why.

    Returns:
        ``(end_state, reason)`` where end_state is one of ``complete``,
        ``interrupted-tool``, ``killed-agents``, ``error-cascade``, ``compacted-idle``,
        ``interrupted-user`` or ``unknown``.
    """
    orphans = [c for c in session.tools if c.err_class == "no-result"]
    if orphans:
        names = ", ".join(sorted({c.name for c in orphans})[:3])
        return ("interrupted-tool", f"{len(orphans)} unresolved tool call(s): {names}")
    if any(e.subtype == "agents_killed" for e in session.sysev):
        return ("killed-agents", "a subagent was killed before returning")
    if _trailing_errors(session.tools) >= THRESHOLDS["error_cascade"]:
        return ("error-cascade", "session ends on consecutive failing tool calls")
    if session.messages and session.messages[-1].role == "assistant":
        api_error = _final_api_error(session)
        if api_error is not None:
            return ("error-cascade", api_error.detail or "final model turn ended in an API error")
        if any(e.subtype == "compact_boundary" for e in session.sysev[-5:]):
            return ("compacted-idle", "went idle shortly after a context compaction")
        return ("complete", "")
    if session.messages and session.messages[-1].role == "user":
        return ("interrupted-user", "last turn is a user message with no reply")
    return ("unknown", "no messages parsed")


#: Precedence order — earlier entries win. Same shape as the error taxonomy's ordered rules:
#: a single signal per session, chosen deterministically, so a downstream skill can key on it.
TAIL_SIGNALS: tuple[str, ...] = (
    "mid_tool", "error_tail", "apology", "completion_marker",
    "next_step_stated", "silent", "neutral",
)

_APOLOGY_RE = re.compile(r"\b(sorry|apologi[sz]e|unable to|can'?t|cannot)\b", re.I)
_COMPLETION_RE = re.compile(
    r"\b(done|complete[ds]?|finished|shipped|ready|works|all set|"
    r"passing|passes)\b", re.I)
_NEXT_STEP_RE = re.compile(
    r"(?:^|[\.\n])\s*(?:I(?:'|’)?ll|I will|next[,:]|next step|going to|"
    r"about to|let me)\b", re.I)


def derive_tail_signal(session: ParsedSession, tail_texts: dict[int, str]) -> str:
    """Classify the tail of a session for the ``unfinished-work`` skill.

    Args:
        session: Post-parse session, with ``err_class`` already annotated on tools.
        tail_texts: Map from ``message.line`` to that message's **full** text — the
            classifier needs uncapped strings because completion markers ("this is now
            done") frequently land past the 200-char preview cap. The caller decides how
            many trailing messages to include; only the entries whose line matches a
            message in ``session.messages`` are consulted.

    Returns:
        One value from ``TAIL_SIGNALS``. Never raises. Empty sessions return ``"neutral"``.
    """
    # mid_tool: any tool_use without a paired tool_result. `annotate_errors` already tags
    # these with err_class == "no-result" (parse.py leaves the flag set to False because
    # the tool did not produce a failing result — it produced no result at all).
    if any(t.err_class == "no-result" for t in session.tools):
        return "mid_tool"

    # error_tail: the final tool call actually failed with a classified taxonomy error.
    if session.tools:
        last = session.tools[-1]
        if last.is_error and last.err_class and last.err_class != "no-result":
            return "error_tail"

    if not session.messages:
        return "neutral"

    last_msg = session.messages[-1]

    # silent: the final turn is a user prompt with no assistant reply — the same signal
    # `derive_end_state` uses for `interrupted-user`, exposed here for the tail classifier.
    if last_msg.role == "user":
        return "silent"

    if last_msg.role != "assistant":
        return "neutral"

    text = tail_texts.get(last_msg.line, last_msg.preview or "")
    if _APOLOGY_RE.search(text):
        return "apology"
    if _COMPLETION_RE.search(text):
        return "completion_marker"
    if _NEXT_STEP_RE.search(text):
        return "next_step_stated"
    return "neutral"


def _trailing_errors(tools: list[ToolCall]) -> int:
    """Count failing tool calls at the tail of the session."""
    count = 0
    for call in reversed(tools):
        if call.is_error:
            count += 1
        else:
            break
    return count


def _repeat_tool(session: ParsedSession) -> list[Anomaly]:
    """Identical (tool, input) invoked repeatedly — the loop signal."""
    seen: dict[tuple[str, str], list[int]] = {}
    for call in session.tools:
        seen.setdefault((call.name, call.input_digest), []).append(call.line)
    out = []
    for (name, _dig), lines in seen.items():
        if len(lines) >= THRESHOLDS["repeat_tool"]:
            out.append(Anomaly("repeat-tool", f"{name} called with identical input",
                               len(lines), lines[:8]))
    return out


def _path_churn(session: ParsedSession, op: str, key: str, kind: str) -> list[Anomaly]:
    """Shared implementation for edit-thrash and read-loop detection."""
    lines: dict[str, list[int]] = {}
    for f in session.files:
        if f.op == op:
            lines.setdefault(f.path, []).append(f.line)
    limit = THRESHOLDS[key]
    return [Anomaly(kind, path, len(ls), ls) for path, ls in lines.items() if len(ls) >= limit]


def _error_cascade(session: ParsedSession) -> list[Anomaly]:
    """Runs of consecutive failing tool calls."""
    out, run, lines = [], 0, []
    for call in session.tools:
        if call.is_error:
            run += 1
            lines.append(call.line)
        else:
            if run >= THRESHOLDS["error_cascade"]:
                out.append(Anomaly("error-cascade", "consecutive failures", run, lines[:8]))
            run, lines = 0, []
    if run >= THRESHOLDS["error_cascade"]:
        out.append(Anomaly("error-cascade", "consecutive failures", run, lines[:8]))
    return out


def _hook_pingpong(session: ParsedSession) -> list[Anomaly]:
    """Repeated hook blocks against the same tool — a self-inflicted retry loop."""
    counts: dict[str, list[int]] = {}
    for call in session.tools:
        if call.err_class == "hook-block":
            counts.setdefault(call.name, []).append(call.line)
    return [Anomaly("hook-pingpong", f"{name} blocked by a hook repeatedly", len(lines),
                    lines[:8])
            for name, lines in counts.items() if len(lines) >= THRESHOLDS["hook_pingpong"]]


def _agent_trouble(session: ParsedSession) -> list[Anomaly]:
    """Killed or never-returning subagents."""
    out = []
    kills = [e.line for e in session.sysev if e.subtype == "agents_killed"]
    if kills:
        out.append(Anomaly("agent-kill", "subagent killed", len(kills), kills[:8]))
    orphans = [c.line for c in session.tools
               if c.name in ("Agent", "subagent") and c.err_class == "no-result"]
    if orphans:
        out.append(Anomaly("orphan-subagent", "Agent spawned, never returned",
                           len(orphans), orphans[:8]))
    return out


def _compaction_churn(session: ParsedSession) -> list[Anomaly]:
    """Repeated context compaction — the session outgrew its window more than once."""
    lines = [e.line for e in session.sysev if e.subtype == "compact_boundary"]
    if len(lines) >= THRESHOLDS["compaction_churn"]:
        return [Anomaly("compaction-churn", "context compacted repeatedly", len(lines),
                        lines[:8])]
    return []


def _stalls(session: ParsedSession) -> list[Anomaly]:
    """Individual tool calls that ran far longer than the threshold."""
    limit = THRESHOLDS["stall_ms"]
    slow = [c for c in session.tools if c.dur_ms is not None and c.dur_ms > limit]
    return [Anomaly("stall", f"{c.name} took {c.dur_ms // 1000}s", 1, [c.line]) for c in slow]


def _rejection_persist(session: ParsedSession) -> list[Anomaly]:
    """The same call retried after the user explicitly rejected it."""
    rejected: set[tuple[str, str]] = set()
    out = []
    for call in session.tools:
        key = (call.name, call.input_digest)
        if key in rejected and not call.is_error:
            out.append(Anomaly("rejection-persist", f"{call.name} retried after rejection",
                               1, [call.line]))
            rejected.discard(key)
        if call.err_class == "user-rejected":
            rejected.add(key)
    return out


_DETECTORS: list[Callable[[ParsedSession], list[Anomaly]]] = [
    _repeat_tool,
    lambda s: _path_churn(s, "edit", "file_thrash", "file-thrash"),
    lambda s: _path_churn(s, "read", "read_loop", "read-loop"),
    _error_cascade,
    _hook_pingpong,
    _agent_trouble,
    _compaction_churn,
    _stalls,
    _rejection_persist,
]


def detect(session: ParsedSession) -> list[Anomaly]:
    """Run every detector against a session.

    Args:
        session: A session whose errors have already been annotated.

    Returns:
        Anomalies sorted by count, most frequent first.
    """
    found: list[Anomaly] = []
    for detector in _DETECTORS:
        found.extend(detector(session))
    return sorted(found, key=lambda a: -a.count)
