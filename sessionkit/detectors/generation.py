"""Generation detectors: what went wrong inside the model's own output."""

from __future__ import annotations

import bisect
import statistics

from sessionkit.detectors.base import MAX_LINES, Anomaly, assistant_turns
from sessionkit.features import parse_ts
from sessionkit.parse import SYNTHETIC_MODEL, Message, ParsedSession
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
    """Each assistant message with the milliseconds since the latest other entry at or
    before it.

    An earlier record stamped the same instant counts (Claude Code's block records of one
    message often share a stamp). Claude Code's ``<synthetic>`` records — written on resume
    or for an API error — are not generations and get no latency. Messages and events with
    unusable timestamps are skipped rather than guessed at.
    """
    entries = [(m.ts, m.line) for m in session.messages]
    entries += [(c.result_ts, c.result_line) for c in session.tools]
    entries += [(e.ts, e.line) for e in session.sysev]
    events = sorted((at, line) for at, line in ((parse_ts(ts), line) for ts, line in entries)
                    if at is not None)
    out: list[tuple[Message, int]] = []
    for message in session.messages:
        if message.role != "assistant" or message.model == SYNTHETIC_MODEL:
            continue
        at = parse_ts(message.ts)
        if at is None:
            continue
        # Transcript line breaks timestamp ties, so an earlier record stamped the same instant
        # counts as "before" and the message's own entry does not.
        index = bisect.bisect_left(events, (at, message.line))
        if index > 0:
            out.append((message, int((at - events[index - 1][0]).total_seconds() * 1000)))
    return out


def generation_stall(session: ParsedSession) -> list[Anomaly]:
    """A model turn that took far longer than both an absolute limit and the session's norm."""
    latencies = assistant_latencies(session)
    if not latencies:
        return []
    has_median = len(latencies) >= 3
    median = statistics.median(ms for _m, ms in latencies) if has_median else 0
    norm = f"session median {int(median) // 1000}s" if has_median else "no session median"
    limit = THRESHOLDS["generation_stall_ms"]
    factor = THRESHOLDS["generation_stall_factor"]
    return [Anomaly("generation-stall", f"model turn took {ms // 60_000} min ({norm})",
                    1, [message.line])
            for message, ms in latencies if ms > limit and ms > factor * median]
