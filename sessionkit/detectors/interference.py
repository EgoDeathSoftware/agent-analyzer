"""Cross-session detector: a session's model errors while another session's long command ran.

This is the only detector that needs more than one session, so it runs as a corpus-level pass
after every session is loaded rather than inside :func:`sessionkit.detectors.detect`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sessionkit.detectors.base import Anomaly
from sessionkit.features import parse_ts
from sessionkit.harness import role
from sessionkit.parse import Message, ParsedSession
from sessionkit.taxonomy import THRESHOLDS


@dataclass
class _Window:
    sid: str
    name: str
    line: int
    start: datetime
    end: datetime


def _long_exec_windows(session: ParsedSession) -> list[_Window]:
    windows = []
    for call in session.tools:
        long_enough = (call.dur_ms or 0) >= THRESHOLDS["interference_exec_ms"]
        if role(session.harness, call.name) != "exec" or not long_enough:
            continue
        start, end = parse_ts(call.ts), parse_ts(call.result_ts)
        if start is not None and end is not None:
            windows.append(_Window(session.sid, call.name, call.line, start, end))
    return windows


def resource_interference(sessions: list[tuple[str, ParsedSession]]) -> list[list[Anomaly]]:
    """For each ``(project_key, session)``, the failed model calls that happened while another
    session in the same project was running a long command.

    Returns:
        One list of findings per input pair, in input order.
    """
    by_project: dict[str, list[int]] = {}
    for index, (project, _session) in enumerate(sessions):
        by_project.setdefault(project, []).append(index)
    # Each session's long-exec windows are computed once here, not once per erroring session
    # that shares its project — a project with several failing siblings would otherwise redo
    # the same scan for every one of them.
    windows_by_index = {index: _long_exec_windows(session)
                        for index, (_project, session) in enumerate(sessions)}
    found: list[list[Anomaly]] = [[] for _ in sessions]
    for index, (project, session) in enumerate(sessions):
        errors = [m for m in session.messages if m.stop_reason == "error"]
        if not errors:
            continue
        windows = [w for other in by_project[project] if other != index
                   for w in windows_by_index[other]]
        found[index] = _overlaps(errors, windows)
    return found


def _overlaps(errors: list[Message], windows: list[_Window]) -> list[Anomaly]:
    out: list[Anomaly] = []
    for message in errors:
        at = parse_ts(message.ts)
        hit = next((w for w in windows if at is not None and w.start <= at <= w.end), None)
        if hit is None:
            continue
        seconds = int((hit.end - hit.start).total_seconds())
        out.append(Anomaly("resource-interference",
                           f"model call failed while session {hit.sid[:8]} ran {hit.name} "
                           f"(line {hit.line}, {seconds}s)", 1, [message.line]))
    return out
