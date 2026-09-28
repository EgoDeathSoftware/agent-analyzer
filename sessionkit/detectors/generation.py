"""Generation detectors: what went wrong inside the model's own output."""

from __future__ import annotations

import bisect
import statistics

from sessionkit.detectors.base import MAX_LINES, Anomaly, assistant_turns
from sessionkit.features import parse_ts
from sessionkit.parse import Message, ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def empty_turn(session: ParsedSession) -> list[Anomaly]:
    """A model message that ended normally with no visible text and no tool call.

    Judged per whole message: Claude Code writes a reasoning-only block record, carrying the
    message's final stop reason, before the record that holds the text.
    """
    hits = [turn[0].line for turn in assistant_turns(session)
            if turn[-1].stop_reason == "end"
            and not any(m.tool_calls or m.text_len for m in turn)]
    if not hits:
        return []
    return [Anomaly("empty-turn", "turn ended with no text and no tool call", len(hits),
                    hits[:MAX_LINES])]


def degeneration(session: ParsedSession) -> list[Anomaly]:
    """A message (visible or reasoning text, any stop reason) that repeats itself."""
    limit = THRESHOLDS["degeneration_ratio"]
    hits = [m for m in session.messages if m.role == "assistant" and m.repeat_ratio >= limit]
    if not hits:
        return []
    worst = max(m.repeat_ratio for m in hits)
    return [Anomaly("degeneration", f"repeated text within one turn (ratio {worst:.2f})",
                    len(hits), [m.line for m in hits][:MAX_LINES])]


def output_truncated(session: ParsedSession) -> list[Anomaly]:
    """Model messages cut off by the output token limit, counted once per message."""
    hits = [turn[0].line for turn in assistant_turns(session)
            if any(m.stop_reason == "length" for m in turn)]
    if not hits:
        return []
    return [Anomaly("output-truncated", "turn hit the output token limit", len(hits),
                    hits[:MAX_LINES])]


def assistant_latencies(session: ParsedSession) -> list[tuple[Message, int]]:
    """Each assistant message with the milliseconds since the entry before it.

    Messages and events with unusable timestamps are skipped rather than guessed at.
    """
    stamps = [m.ts for m in session.messages]
    stamps += [c.result_ts for c in session.tools]
    stamps += [e.ts for e in session.sysev]
    events = sorted(t for t in (parse_ts(s) for s in stamps) if t is not None)
    out: list[tuple[Message, int]] = []
    for message in session.messages:
        at = parse_ts(message.ts) if message.role == "assistant" else None
        if at is None:
            continue
        index = bisect.bisect_left(events, at)
        if index > 0:
            out.append((message, int((at - events[index - 1]).total_seconds() * 1000)))
    return out


def generation_stall(session: ParsedSession) -> list[Anomaly]:
    """A model turn that took far longer than both an absolute limit and the session's norm."""
    latencies = assistant_latencies(session)
    if not latencies:
        return []
    median = statistics.median(ms for _m, ms in latencies) if len(latencies) >= 3 else 0
    limit = THRESHOLDS["generation_stall_ms"]
    factor = THRESHOLDS["generation_stall_factor"]
    return [Anomaly("generation-stall",
                    f"model turn took {ms // 60_000} min (session median {int(median) // 1000}s)",
                    1, [message.line])
            for message, ms in latencies if ms > limit and ms > factor * median]
