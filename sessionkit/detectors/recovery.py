"""Recovery candidates (tier S->J): how the agent handled its own failures."""

from __future__ import annotations

from sessionkit.classify import tail_text_signal, trailing_errors
from sessionkit.detectors.base import Anomaly, assistant_turns
from sessionkit.parse import ParsedSession, ToolCall

#: Failures the agent is not expected to recover from: the user or a sibling call caused them.
_NOT_THE_AGENTS = ("aborted", "sibling-cascade", "user-rejected")


def error_ignored(session: ParsedSession) -> list[Anomaly]:
    """A failed call, then a next assistant turn claiming success with no retry in between.

    Claude Code splits one model message into several records sharing a ``msg_id`` (a
    thinking-only record followed by the text record that actually claims completion); the
    turn counts as a claim if any of its records does, and the finding cites that record's
    line, not the turn's first line.
    """
    turns = assistant_turns(session)
    out: list[Anomaly] = []
    for call in session.tools:
        if not call.is_error or call.err_class in _NOT_THE_AGENTS:
            continue
        after = call.result_line or call.line
        turn = next((t for t in turns if t[0].line > after), None)
        if turn is None:
            continue
        claim = next((m for m in turn if m.claims_done), None)
        if claim is None or _retried(session.tools, call, claim.line):
            continue
        out.append(Anomaly("error-ignored",
                           f"{call.name} failed ({call.err_class or 'error'}); the next message "
                           "claims success", 1, [call.line, claim.line]))
    return out


def _retried(tools: list[ToolCall], failed: ToolCall, until: int) -> bool:
    return any(c is not failed and c.name == failed.name and failed.line < c.line <= until
               for c in tools)


def _lone_final_failure(session: ParsedSession) -> ToolCall | None:
    """The session's last call, if it failed on its own (not in a cascade) and it was the
    agent's failure."""
    if not session.tools:
        return None
    last = session.tools[-1]
    if not last.is_error or last.err_class in _NOT_THE_AGENTS:
        return None
    return last if trailing_errors(session.tools) == 1 else None


def give_up(session: ParsedSession) -> list[Anomaly]:
    """One failure, then the session ends with an apology or without comment."""
    failed = _lone_final_failure(session)
    if failed is None or not session.messages or session.end_state.startswith("interrupted"):
        return []
    final = session.messages[-1]
    if final.role != "assistant" or final.line <= (failed.result_line or failed.line):
        return []
    if tail_text_signal(session.final_text or final.preview) not in ("apology", "neutral"):
        return []
    return [Anomaly("give-up",
                    f"stopped after a single {failed.name} failure "
                    f"({failed.err_class or 'error'})", 1, [failed.line, final.line])]
