"""Loop detectors: the same call, file or failure, over and over."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.harness import call_paths, role
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def repeat_tool(session: ParsedSession) -> list[Anomaly]:
    """Identical (tool, input) invoked repeatedly — the loop signal. Polling tools exist to
    be called repeatedly, so role ``poll`` is exempt."""
    seen: dict[tuple[str, str], list[int]] = {}
    for call in session.tools:
        if role(session.harness, call.name) == "poll":
            continue
        seen.setdefault((call.name, call.input_digest), []).append(call.line)
    out = []
    for (name, _digest), lines in seen.items():
        if len(lines) >= THRESHOLDS["repeat_tool"]:
            out.append(Anomaly("repeat-tool", f"{name} called with identical input",
                               len(lines), lines[:MAX_LINES]))
    return out


def file_thrash(session: ParsedSession) -> list[Anomaly]:
    """The same path edited many times, or several times with failures in between.

    Clean iterative edits are normal work: at the old threshold of 5, only 14 of 145 corpus
    hits involved a real edit failure.
    """
    failed = {c.tool_use_id for c in session.tools if c.is_error}
    edits: dict[str, list[int]] = {}
    failures: dict[str, int] = {}
    for f in session.files:
        if f.op != "edit":
            continue
        edits.setdefault(f.path, []).append(f.line)
        if f.tool_use_id in failed:
            failures[f.path] = failures.get(f.path, 0) + 1
    out = []
    for path, lines in edits.items():
        many = len(lines) >= THRESHOLDS["file_thrash"]
        failing = len(lines) >= THRESHOLDS["file_thrash_min"] and \
            failures.get(path, 0) >= THRESHOLDS["file_thrash_failures"]
        if many or failing:
            out.append(Anomaly("file-thrash", path, len(lines), lines))
    return out


def read_loop(session: ParsedSession) -> list[Anomaly]:
    """The same path read repeatedly. A re-read after that path was edited, or after a
    compaction erased the model's copy, is a legitimate refresh and is not counted."""
    events = sorted([(f.line, 1, f) for f in session.files]
                    + [(e.line, 0, e) for e in session.sysev if e.subtype == "compaction"],
                    key=lambda e: (e[0], e[1]))
    counted: dict[str, list[int]] = {}
    edited: set[str] = set()
    read_at_compaction: dict[str, int] = {}
    compactions = 0
    for line, kind, item in events:
        if kind == 0:
            compactions += 1
            continue
        if item.op == "edit":
            edited.add(item.path)
            continue
        if item.op != "read":
            continue
        refresh = item.path in edited or \
            read_at_compaction.get(item.path, compactions) < compactions
        edited.discard(item.path)
        read_at_compaction[item.path] = compactions
        if not refresh:
            counted.setdefault(item.path, []).append(line)
    limit = THRESHOLDS["read_loop"]
    return [Anomaly("read-loop", path, len(ls), ls)
            for path, ls in counted.items() if len(ls) >= limit]


def error_cascade(session: ParsedSession) -> list[Anomaly]:
    """Runs of consecutive failing tool calls. A sibling-cascade failure only echoes another
    call's error, so it neither extends nor breaks a run."""
    out: list[Anomaly] = []
    run, lines = 0, []
    for call in session.tools:
        if call.err_class == "sibling-cascade":
            continue
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


def edit_mismatch_spiral(session: ParsedSession) -> list[Anomaly]:
    """Edit-mismatch failures piling up on one file."""
    by_path: dict[str, list[int]] = {}
    for call in session.tools:
        if call.err_class == "edit-mismatch":
            paths = call_paths(session.harness, call)
            by_path.setdefault(paths[0] if paths else call.name, []).append(call.line)
    limit = THRESHOLDS["edit_mismatch_spiral"]
    return [Anomaly("edit-mismatch-spiral", path, len(ls), ls[:MAX_LINES])
            for path, ls in by_path.items() if len(ls) >= limit]


_READ_ONLY = ("read", "search")
_PROGRESS = ("edit", "write", "exec")


def read_only_spiral(session: ParsedSession) -> list[Anomaly]:
    """A long run of read/search calls with no edit, command, or substantial answer."""
    events = sorted([(m.line, 0, m) for m in session.messages if m.role == "assistant"]
                    + [(c.line, 1, c) for c in session.tools], key=lambda e: (e[0], e[1]))
    out: list[Anomaly] = []
    run: list[int] = []
    for _line, kind, item in events:
        if kind == 0:
            if item.text_len >= THRESHOLDS["read_only_text_chars"]:
                out.extend(_spiral(run))
                run = []
            continue
        call_role = role(session.harness, item.name)
        if call_role in _READ_ONLY:
            run.append(item.line)
        elif call_role in _PROGRESS:
            out.extend(_spiral(run))
            run = []
    out.extend(_spiral(run))
    return out


def _spiral(run: list[int]) -> list[Anomaly]:
    if len(run) < THRESHOLDS["read_only_spiral"]:
        return []
    return [Anomaly("read-only-spiral",
                    f"{len(run)} consecutive read/search calls with no edit, command or answer",
                    len(run), run[:MAX_LINES])]


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
