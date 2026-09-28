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
]


def detect(session: ParsedSession) -> list[Anomaly]:
    """Run every detector against a session.

    Args:
        session: A session whose errors have already been annotated.

    Returns:
        Anomalies sorted by count, most frequent first.
    """
    found: list[Anomaly] = []
    for detector in DETECTORS:
        found.extend(detector(session))
    return sorted(found, key=lambda a: -a.count)


__all__ = ["Anomaly", "detect"]
