"""Anomaly detectors: pure functions from a parsed session to its findings.

Each module groups one failure family (spec §2.2). A detector reads only the session plus the
taxonomy and harness tables — never a file — so the same functions work on a partial session.
"""

from __future__ import annotations

from typing import Callable

from sessionkit.detectors import generation, lifecycle, loops, tool_protocol
from sessionkit.detectors.base import Anomaly
from sessionkit.parse import ParsedSession

DETECTORS: list[Callable[[ParsedSession], list[Anomaly]]] = [
    loops.repeat_tool,
    loops.file_thrash,
    loops.read_loop,
    loops.error_cascade,
    loops.hook_pingpong,
    lifecycle.agent_trouble,
    lifecycle.compaction_churn,
    lifecycle.stalls,
    loops.rejection_persist,
    loops.edit_mismatch_spiral,
    loops.read_only_spiral,
    tool_protocol.error_classes,
    tool_protocol.tool_text_leak,
    generation.empty_turn,
    generation.degeneration,
    generation.output_truncated,
    generation.generation_stall,
    lifecycle.user_interrupt,
]


def detect(session: ParsedSession, live: bool = False) -> list[Anomaly]:
    """Run every detector against a session.

    Args:
        session: A session whose errors and end state have already been derived.
        live: Whether the session is still running. A live session's unfinished tail is
            expected, so the hang and truncation detectors stay silent for it.

    Returns:
        Anomalies sorted by count, most frequent first.
    """
    found: list[Anomaly] = []
    for detector in DETECTORS:
        found.extend(detector(session))
    found.extend(lifecycle.hung(session, live))
    found.extend(lifecycle.abrupt_truncation(session, live))
    return sorted(found, key=lambda a: -a.count)


__all__ = ["Anomaly", "detect"]
