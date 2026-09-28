"""Session end-state derivation and tail signals.

The failure taxonomy lives in ``sessionkit.taxonomy`` and the anomaly detectors in
``sessionkit.detectors``; this module decides how a session *ended*.
"""

from __future__ import annotations

import re

from sessionkit.parse import ParsedSession, SysEvent, ToolCall
from sessionkit.taxonomy import THRESHOLDS, classify_error, signature


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
    if trailing_errors(session.tools) >= THRESHOLDS["error_cascade"]:
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


def trailing_errors(tools: list[ToolCall]) -> int:
    """Count failing tool calls at the tail of the session."""
    count = 0
    for call in reversed(tools):
        if call.is_error:
            count += 1
        else:
            break
    return count
