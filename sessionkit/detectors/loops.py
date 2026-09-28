"""Loop detectors: the same call, file or failure, over and over."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def repeat_tool(session: ParsedSession) -> list[Anomaly]:
    """Identical (tool, input) invoked repeatedly — the loop signal."""
    seen: dict[tuple[str, str], list[int]] = {}
    for call in session.tools:
        seen.setdefault((call.name, call.input_digest), []).append(call.line)
    out = []
    for (name, _digest), lines in seen.items():
        if len(lines) >= THRESHOLDS["repeat_tool"]:
            out.append(Anomaly("repeat-tool", f"{name} called with identical input",
                               len(lines), lines[:MAX_LINES]))
    return out


def _path_churn(session: ParsedSession, op: str, key: str, kind: str) -> list[Anomaly]:
    """Shared implementation for edit-thrash and read-loop detection."""
    lines: dict[str, list[int]] = {}
    for f in session.files:
        if f.op == op:
            lines.setdefault(f.path, []).append(f.line)
    limit = THRESHOLDS[key]
    return [Anomaly(kind, path, len(ls), ls) for path, ls in lines.items() if len(ls) >= limit]


def file_thrash(session: ParsedSession) -> list[Anomaly]:
    """The same path edited repeatedly."""
    return _path_churn(session, "edit", "file_thrash", "file-thrash")


def read_loop(session: ParsedSession) -> list[Anomaly]:
    """The same path read repeatedly."""
    return _path_churn(session, "read", "read_loop", "read-loop")


def error_cascade(session: ParsedSession) -> list[Anomaly]:
    """Runs of consecutive failing tool calls."""
    out: list[Anomaly] = []
    run, lines = 0, []
    for call in session.tools:
        if call.is_error:
            run += 1
            lines.append(call.line)
        else:
            if run >= THRESHOLDS["error_cascade"]:
                out.append(Anomaly("error-cascade", "consecutive failures", run,
                                   lines[:MAX_LINES]))
            run, lines = 0, []
    if run >= THRESHOLDS["error_cascade"]:
        out.append(Anomaly("error-cascade", "consecutive failures", run, lines[:MAX_LINES]))
    return out


def hook_pingpong(session: ParsedSession) -> list[Anomaly]:
    """Repeated hook blocks against the same tool — a self-inflicted retry loop."""
    counts: dict[str, list[int]] = {}
    for call in session.tools:
        if call.err_class == "hook-block":
            counts.setdefault(call.name, []).append(call.line)
    return [Anomaly("hook-pingpong", f"{name} blocked by a hook repeatedly", len(lines),
                    lines[:MAX_LINES])
            for name, lines in counts.items() if len(lines) >= THRESHOLDS["hook_pingpong"]]


def rejection_persist(session: ParsedSession) -> list[Anomaly]:
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
