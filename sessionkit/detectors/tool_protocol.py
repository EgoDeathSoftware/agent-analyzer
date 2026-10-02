"""Tool-protocol detectors: tool calls that failed as calls, not as the work they did."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly, assistant_turns
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import classify_api_error, lookup

#: Error classes in these families also surface as session findings (spec §2).
SURFACED_FAMILIES = ("tool-protocol", "generation", "environment")


def _surfaced(name: str) -> bool:
    """Surface a class only if it names something fixable (fix target is not ``none``)."""
    entry = lookup(name)
    return (entry is not None and entry.family in SURFACED_FAMILIES
            and entry.fix_target != "none")


def error_classes(session: ParsedSession) -> list[Anomaly]:
    """One finding per surfaced error class present: failed tool calls and failed model
    calls (an assistant message whose ``stop_reason`` is ``error``)."""
    lines: dict[str, list[int]] = {}
    for call in session.tools:
        if call.is_error and _surfaced(call.err_class):
            lines.setdefault(call.err_class, []).append(call.line)
    for message in session.messages:
        if message.stop_reason == "error" and message.error_text:
            kind = classify_api_error(message.error_text)
            if _surfaced(kind):
                lines.setdefault(kind, []).append(message.line)
    return [Anomaly(kind, f"{len(found)} occurrence(s)", len(found), found[:MAX_LINES])
            for kind, found in lines.items()]


def tool_text_leak(session: ParsedSession) -> list[Anomaly]:
    """A model message carrying tool-call markup while none of its records made a call."""
    hits = [next(m for m in turn if m.markup_leak) for turn in assistant_turns(session)
            if any(m.markup_leak for m in turn) and not any(m.tool_calls for m in turn)]
    if not hits:
        return []
    formats = ", ".join(sorted({m.markup_leak for m in hits}))
    return [Anomaly("tool-text-leak", f"tool-call markup in plain text ({formats})",
                    len(hits), [m.line for m in hits][:MAX_LINES])]
