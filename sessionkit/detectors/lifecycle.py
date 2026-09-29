"""Lifecycle detectors: how a session's calls and subagents ran, stalled or ended."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.harness import declared_timeout_ms, is_interrupt_marker, role
from sessionkit.parse import Message, ParsedSession, ToolCall
from sessionkit.taxonomy import THRESHOLDS


def agent_trouble(session: ParsedSession) -> list[Anomaly]:
    """Killed or never-returning subagents."""
    out = []
    kills = [e.line for e in session.sysev if e.subtype == "agents_killed"]
    if kills:
        out.append(Anomaly("agent-kill", "subagent killed", len(kills), kills[:MAX_LINES]))
    orphans = [c.line for c in session.tools
               if role(session.harness, c.name) == "dispatch" and c.err_class == "no-result"]
    if orphans:
        out.append(Anomaly("orphan-subagent", "Agent spawned, never returned",
                           len(orphans), orphans[:MAX_LINES]))
    return out


def compaction_churn(session: ParsedSession) -> list[Anomaly]:
    """Repeated context compaction — the session outgrew its window more than once."""
    lines = [e.line for e in session.sysev if e.subtype == "compaction"]
    if len(lines) >= THRESHOLDS["compaction_churn"]:
        return [Anomaly("compaction-churn", "context compacted repeatedly", len(lines),
                        lines[:MAX_LINES])]
    return []


#: Their duration measures a human or a background job, not the tool.
_NO_STALL_ROLES = ("ask-user", "dispatch", "poll", "edit", "write")


def stalls(session: ParsedSession) -> list[Anomaly]:
    """Tool calls that ran far longer than they should have (spec §2.2)."""
    return [Anomaly("stall", f"{c.name} took {c.dur_ms // 1000}s", 1, [c.line])
            for c in session.tools if c.dur_ms is not None and _is_stall(session.harness, c)]


#: Claude Code's call duration includes the wait for the user's permission approval.
_TIMEOUT_OVERRUN = 1.05
_REMOTE_SEARCH = frozenset({"WebSearch", "WebFetch"})


def _duration_is_approval_wait(harness: str, call: ToolCall, call_role: str) -> bool:
    """Whether a Claude Code call's duration cannot have been the tool's own."""
    if harness != "claude-code":
        return False
    if call_role in ("read", "search"):
        return call.name not in _REMOTE_SEARCH
    if call_role == "exec":
        timeout = declared_timeout_ms(harness, call)
        return bool(timeout) and call.dur_ms > _TIMEOUT_OVERRUN * timeout
    return False


def _is_stall(harness: str, call: ToolCall) -> bool:
    call_role = role(harness, call.name)
    if call_role in _NO_STALL_ROLES or _duration_is_approval_wait(harness, call, call_role):
        return False
    if call_role != "exec":
        return call.dur_ms > THRESHOLDS["stall_ms"]
    timeout = declared_timeout_ms(harness, call)
    if timeout:
        return call.dur_ms >= THRESHOLDS["stall_timeout_fraction"] * timeout
    return call.dur_ms > THRESHOLDS["stall_exec_ms"]


def hung(session: ParsedSession, live: bool) -> list[Anomaly]:
    """A finished (not live) session that ended waiting on a call or an unfinished turn."""
    if live:
        return []
    if session.end_state == "interrupted-tool":
        pending = [c for c in session.tools if c.err_class == "no-result"
                   and role(session.harness, c.name) not in ("ask-user", "dispatch")]
        if pending:
            return [Anomaly("hung", f"ended waiting on {pending[-1].name}", len(pending),
                            [c.line for c in pending][:MAX_LINES])]
    # Only pi persists an unfinished stream as an assistant message with no stop reason;
    # Claude Code writes stop_reason null on every intermediate per-block record.
    last = session.messages[-1] if session.messages else None
    if (session.harness == "pi" and last is not None and last.role == "assistant"
            and last.stop_reason == ""):
        return [Anomaly("hung", "final model turn never finished streaming", 1, [last.line])]
    return []


def abrupt_truncation(session: ParsedSession, live: bool) -> list[Anomaly]:
    """The transcript's last line is a partial write and nothing is still writing it."""
    if live or not session.last_line_malformed:
        return []
    return [Anomaly("abrupt-truncation", "last transcript line is a partial write", 1, [])]


def interrupt_points(session: ParsedSession) -> list[int]:
    """Lines where the user stopped the agent: an aborted turn or call, or Claude Code's
    synthetic "[Request interrupted by user]" message."""
    points = {m.line for m in session.messages
              if m.role == "assistant" and m.stop_reason == "aborted"}
    points |= {c.line for c in session.tools if c.err_class == "aborted"}
    points |= {m.line for m in session.messages
               if m.role == "user" and is_interrupt_marker(m.preview)}
    return sorted(points)


def _next_prompt(session: ParsedSession, after: int) -> Message | None:
    return next((m for m in session.messages if m.is_prompt and m.line > after
                 and not is_interrupt_marker(m.preview)), None)


def user_interrupt(session: ParsedSession) -> list[Anomaly]:
    """The user stopped the agent and then told it something new — a redirect."""
    out: list[Anomaly] = []
    seen: set[int] = set()
    for point in interrupt_points(session):
        redirect = _next_prompt(session, point)
        if redirect is None or redirect.line in seen:
            continue
        seen.add(redirect.line)
        out.append(Anomaly("user-interrupt",
                           f"the user stopped the agent and redirected at line {redirect.line}",
                           1, [point, redirect.line]))
    return out
