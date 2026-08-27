"""Aggregations over an in-memory :class:`~sessionkit.corpus.Corpus`.

These replace the SQL that used to run against the derived cache. Each function returns plain
dicts keyed exactly as the old ``SELECT`` aliases were, so the rendering code in ``cli.py``
does not care where its rows came from.

Where a query mirrors former SQL, the semantics are reproduced deliberately rather than
approximated — ``MIN(output_preview)`` picked the *lexicographically* smallest exemplar in a
group, not the first-seen one, and reports would shift if that became ``next(iter(...))``.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sessionkit import settingsjson as sj
from sessionkit.classify import ANOMALY_HINTS, signature
from sessionkit.corpus import Corpus, Loaded
from sessionkit.parse import ToolCall

Row = dict[str, Any]


def is_failure(tool: ToolCall) -> bool:
    """Whether a tool call counts as a failure.

    A call with no result at all is a failure even though nothing set ``is_error`` — the
    session was interrupted mid-call. This predicate was duplicated in two SQL statements;
    keeping it in one place stops the two drifting.
    """
    return tool.is_error or tool.err_class == "no-result"


@dataclass
class Filter:
    """The shared scope flags: ``--since``/``--project``/``--source``/``--state``."""

    cutoff: str = ""
    project: str = ""
    source: str = ""
    state: str = ""
    subagents: str = "include"

    def matches(self, entry: Loaded) -> bool:
        """Whether a session is in scope."""
        session = entry.session
        if self.cutoff and not (session.ended_at or "") >= self.cutoff:
            return False
        if self.project and entry.project_key != self.project.lower():
            return False
        if self.source and session.source_id != self.source:
            return False
        if self.state and session.end_state != self.state:
            return False
        if self.subagents == "exclude" and session.is_subagent:
            return False
        if self.subagents == "only" and not session.is_subagent:
            return False
        return True

    def apply(self, corpus: Corpus) -> list[Loaded]:
        """Every in-scope session, in load order."""
        return [e for e in corpus.sessions if self.matches(e)]


# --- doctor -----------------------------------------------------------------------------

def source_rows(corpus: Corpus) -> list[Row]:
    """Sources, reachable first, then by id."""
    ordered = sorted(corpus.sources, key=lambda s: (s.reachable is False, s.id))
    return [{"id": s.id, "kind": s.kind, "location": s.location,
             "reachable": bool(s.reachable), "root": str(s.root), "note": s.note}
            for s in ordered]


def retention_days(corpus: Corpus) -> dict[str, int | None]:
    """Effective ``cleanupPeriodDays`` per reachable source (PLAN.md §3.3).

    "No errors before March" and "transcripts before March were deleted" render identically
    without this — it belongs next to reachability in ``sk doctor``, not buried in a config dump.
    """
    return {s.id: sj.cleanup_period_days(sj.read_settings(s.root)) for s in _config_sources(corpus)}


def totals(corpus: Corpus) -> Row:
    """Whole-corpus rollup. Unscoped: ``doctor`` always reports everything it could read."""
    sessions = [e.session for e in corpus.sessions]
    starts = [s.started_at for s in sessions if s.started_at]
    ends = [s.ended_at for s in sessions if s.ended_at]
    calls = sum(len(s.tools) for s in sessions)
    errors = sum(1 for s in sessions for t in s.tools if t.is_error)
    return {
        "n": len(sessions),
        "turns": sum(s.turns for s in sessions),
        "cost": sum(s.cost_usd for s in sessions),
        "first": min(starts) if starts else "",
        "last": max(ends) if ends else "",
        "calls": calls,
        "errors": errors,
    }


# --- index ------------------------------------------------------------------------------

def index_rows(corpus: Corpus, scope: Filter) -> list[Row]:
    """One row per in-scope session, newest first.

    Sessions with no end timestamp sort last, matching SQLite's placement of NULLs under
    ``ORDER BY ended_at DESC``. A naive ``sorted(key=...)`` over the raw value would raise on
    the empty string mixing with real timestamps under some comparisons, so it is coalesced.
    """
    rows = scope.apply(corpus)
    rows.sort(key=lambda e: e.session.ended_at or "", reverse=True)
    out: list[Row] = []
    for entry in rows:
        s = entry.session
        out.append({
            "sid": s.sid,
            "project_key": entry.project_key,
            "ended_at": s.ended_at,
            "turns": s.turns,
            "cost_usd": s.cost_usd,
            "end_state": s.end_state,
            "model": s.model,
            "label": s.title or s.first_prompt,
        })
    return out


# --- errors -----------------------------------------------------------------------------

_GROUP_KEY: dict[str, Callable[[Loaded, ToolCall], str]] = {
    "class": lambda e, t: t.err_class or "other",
    "tool": lambda e, t: t.name,
    "signature": lambda e, t: t.err_detail,
    "session": lambda e, t: e.session.sid,
}


def _failures(scoped: Iterable[Loaded]) -> Iterable[tuple[Loaded, ToolCall]]:
    for entry in scoped:
        for tool in entry.session.tools:
            if is_failure(tool):
                yield entry, tool


def clusters(corpus: Corpus, scope: Filter, group_by: str) -> list[Row]:
    """Failed tool calls grouped by class, tool, signature or session.

    Ordered by count descending, ties broken alphabetically on the bucket.

    The tie-break is a deliberate behaviour change. The former ``ORDER BY n DESC`` left equal
    counts in whatever order SQLite's sorter produced — measured against this corpus it matched
    neither insertion nor alphabetical order, so it was reproducible only by accident. A total
    ordering means two runs over an unchanged corpus cannot disagree.
    """
    key_of = _GROUP_KEY[group_by]
    buckets: dict[str, dict[str, Any]] = {}
    for entry, tool in _failures(scope.apply(corpus)):
        key = key_of(entry, tool)
        bucket = buckets.setdefault(key, {"bucket": key, "n": 0, "sids": set(),
                                          "names": [], "previews": []})
        bucket["n"] += 1
        bucket["sids"].add(entry.session.sid)
        bucket["names"].append(tool.name)
        bucket["previews"].append(tool.output_preview)
    out = [{"bucket": b["bucket"], "n": b["n"], "sessions": len(b["sids"]),
            "tool": min(b["names"]) if b["names"] else "",
            "exemplar": min(b["previews"]) if b["previews"] else ""}
           for b in buckets.values()]
    out.sort(key=lambda r: (-r["n"], r["bucket"] or ""))
    return out


def tool_call_count(corpus: Corpus, scope: Filter) -> int:
    """Every tool call in scope, failed or not — the denominator for a failure rate."""
    return sum(len(e.session.tools) for e in scope.apply(corpus))


def session_count(corpus: Corpus, scope: Filter) -> int:
    """Sessions in scope — the denominator for 'widest reach'."""
    return len(scope.apply(corpus))


def dominant_signature(corpus: Corpus, scope: Filter) -> Row | None:
    """The single most common error signature in scope.

    Note the predicate is stricter than :func:`clusters`: a genuine ``is_error`` with a
    non-empty detail, so an interrupted call with no result never becomes the headline.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for entry in scope.apply(corpus):
        for tool in entry.session.tools:
            if not tool.is_error or not tool.err_detail:
                continue
            bucket = buckets.setdefault(tool.err_detail,
                                        {"bucket": tool.err_detail, "n": 0, "sids": set()})
            bucket["n"] += 1
            bucket["sids"].add(entry.session.sid)
    if not buckets:
        return None
    best = min(buckets.values(), key=lambda b: (-b["n"], b["bucket"]))  # ties: alphabetical
    return {"bucket": best["bucket"], "n": best["n"], "sessions": len(best["sids"])}


# --- show -------------------------------------------------------------------------------

def find_session(corpus: Corpus, prefix: str) -> Loaded | None:
    """Resolve a session id prefix, preferring the most recent match.

    Matching is case-insensitive, as the former ``LIKE`` was for ASCII. Unlike ``LIKE`` this
    treats ``%`` and ``_`` literally, which is what a user typing a session id prefix means.
    """
    needle = prefix.lower()
    matches = [e for e in corpus.sessions if e.session.sid.lower().startswith(needle)]
    if not matches:
        return None
    return max(matches, key=lambda e: e.session.ended_at or "")


def anomaly_rows(entry: Loaded) -> list[Row]:
    """Detected anomalies, most frequent first."""
    ordered = sorted(entry.anomalies, key=lambda a: -a.count)
    return [{"kind": a.kind, "detail": a.detail, "count": a.count,
             "lines": ",".join(str(n) for n in a.lines)} for a in ordered]


def _skill_name(tool: ToolCall) -> str:
    """The skill name from a ``Skill`` tool call's input, best-effort."""
    try:
        parsed = json.loads(tool.input_preview)
    except (ValueError, TypeError):
        return "?"
    return str(parsed.get("skill") or "?") if isinstance(parsed, dict) else "?"


def skill_rows(entry: Loaded) -> list[Row]:
    """Skill invocations in this session, most-used first, ties alphabetical."""
    counts: dict[str, dict[str, Any]] = {}
    for tool in entry.session.tools:
        if tool.name != "Skill":
            continue
        name = _skill_name(tool)
        row = counts.setdefault(name, {"skill": name, "n": 0, "errs": 0})
        row["n"] += 1
        row["errs"] += int(tool.is_error)
    out = list(counts.values())
    out.sort(key=lambda r: (-r["n"], r["skill"] or ""))
    return out


def tool_totals(entry: Loaded) -> list[Row]:
    """Per-tool call and error counts, busiest first, ties alphabetical (see :func:`clusters`)."""
    counts: dict[str, dict[str, Any]] = {}
    for tool in entry.session.tools:
        row = counts.setdefault(tool.name, {"name": tool.name, "n": 0, "errs": 0})
        row["n"] += 1
        row["errs"] += int(tool.is_error)
    out = list(counts.values())
    out.sort(key=lambda r: (-r["n"], r["name"] or ""))
    return out


def timeline_rows(entry: Loaded) -> list[Row]:
    """Messages, tool calls and system events interleaved in line order.

    Built in the same order the former ``UNION ALL`` arms were, then stably sorted, so events
    sharing a line number keep their previous relative order.
    """
    rows: list[Row] = []
    session = entry.session
    for message in session.messages:
        rows.append({"line": message.line, "kind": "msg", "name": message.role,
                     "detail": message.preview})
    for tool in session.tools:
        detail = f"[{tool.err_class}] {tool.err_detail}" if tool.is_error else tool.input_preview
        rows.append({"line": tool.line, "kind": "tool", "name": tool.name, "detail": detail})
    for event in session.sysev:
        rows.append({"line": event.line, "kind": "sys", "name": event.subtype,
                     "detail": event.detail})
    rows.sort(key=lambda r: r["line"])
    return rows


def message_rows(entry: Loaded, lo: int, hi: int) -> list[Row]:
    """Messages within an inclusive line range."""
    return [{"line": m.line, "role": m.role, "text_len": m.text_len, "preview": m.preview}
            for m in entry.session.messages if lo <= m.line <= hi]


def tool_rows(entry: Loaded) -> list[Row]:
    """Every tool call in the session, in line order."""
    return [{"line": t.line, "name": t.name, "dur_ms": t.dur_ms, "err_class": t.err_class,
             "input_preview": t.input_preview} for t in entry.session.tools]


def error_rows(entry: Loaded) -> list[Row]:
    """Only the failing tool calls."""
    return [{"line": t.line, "name": t.name, "err_class": t.err_class,
             "output_preview": t.output_preview}
            for t in entry.session.tools if is_failure(t)]


# --- commands -----------------------------------------------------------------------------

def _owner_sid(entry: Loaded) -> str:
    """The top-level session a call attributes to: a subagent's calls belong to its parent."""
    return entry.session.parent_sid if entry.session.is_subagent else entry.session.sid


def command_rows(corpus: Corpus, scope: Filter, group_by: str,
                 agent_type: str = "") -> list[Row]:
    """Every tool call in scope, grouped by normalised command, tool, owning session or agent.

    ``agent`` grouping includes only calls made by a subagent — the parent session's own direct
    calls have no ``agentId`` and are not a pseudo-agent. Without this restriction, a corpus with
    no subagents would silently fall back to reporting the parent's calls under this grouping,
    which is exactly the failure mode this command exists to avoid (PLAN.md §7).
    """
    entries = scope.apply(corpus)
    if group_by == "agent":
        entries = [e for e in entries if e.session.is_subagent]
    if agent_type:
        entries = [e for e in entries if e.session.agent_type == agent_type]

    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for tool in entry.session.tools:
            if group_by == "command":
                key = f"{tool.name} {signature(tool.input_preview, 120)}"
            elif group_by == "tool":
                key = tool.name
            elif group_by == "agent":
                key = entry.session.sid
            else:  # session
                key = _owner_sid(entry)
            bucket = buckets.setdefault(key, {
                "bucket": key, "n": 0, "sids": set(), "errors": 0, "durs": [],
                "tools": [], "previews": [],
                "agent_type": entry.session.agent_type if entry.session.is_subagent else "",
            })
            bucket["n"] += 1
            bucket["sids"].add(_owner_sid(entry))
            bucket["errors"] += int(is_failure(tool))
            if tool.dur_ms is not None:
                bucket["durs"].append(tool.dur_ms)
            bucket["tools"].append(tool.name)
            bucket["previews"].append(tool.input_preview)

    out = []
    for b in buckets.values():
        out.append({
            "bucket": b["bucket"],
            "tool": min(b["tools"]) if b["tools"] else "",
            "n": b["n"],
            "sessions": len(b["sids"]),
            "errors": b["errors"],
            "avg_ms": int(sum(b["durs"]) / len(b["durs"])) if b["durs"] else None,
            "agent_type": b["agent_type"],
            "exemplar": min(b["previews"]) if b["previews"] else "",
        })
    out.sort(key=lambda r: (-r["n"], r["bucket"] or ""))
    return out


def has_subagents(corpus: Corpus, scope: Filter) -> bool:
    """Whether any subagent transcript is in scope, for the ``sk commands --group-by agent``
    empty-result message: a corpus with none must say so rather than look like a filter miss."""
    return any(e.session.is_subagent for e in scope.apply(corpus))


# --- hooks ----------------------------------------------------------------------------------

def _config_sources(corpus: Corpus) -> list[Any]:
    """Reachable, single-directory sources — the ones with a ``settings.json`` to read.

    A store-set parent has no ``settings.json`` of its own; its children (already expanded by
    ``sources.discover``) do.
    """
    return [s for s in corpus.sources if s.reachable and s.layout != "store-set"]


def _all_hook_defs(corpus: Corpus) -> list[sj.HookDef]:
    """Hook definitions from every reachable source, deduplicated by shape."""
    seen: dict[tuple[str, str, str], sj.HookDef] = {}
    for source in _config_sources(corpus):
        for hook in sj.hook_defs(sj.read_settings(source.root)):
            seen.setdefault((hook.event, hook.matcher, hook.command), hook)
    return list(seen.values())


def _match_hook(output_preview: str, defs: list[sj.HookDef]) -> sj.HookDef | None:
    """The hook whose echoed message appears in this failure's output, if any."""
    body = output_preview or ""
    for hook in defs:
        for message in hook.messages:
            if message and message in body:
                return hook
    return None


def hook_rows(corpus: Corpus, scope: Filter) -> list[Row]:
    """``hook-block`` failures, attributed to the specific ``settings.json`` rule that fired.

    A failure whose message matches no configured hook's echoed text lands under the
    ``unattributed`` event — a taxonomy gap to report, not to hide (mirrors ``classify.py``'s
    ``other`` bucket for the same reason).
    """
    defs = _all_hook_defs(corpus)
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in scope.apply(corpus):
        for tool in entry.session.tools:
            if tool.err_class != "hook-block":
                continue
            match = _match_hook(tool.output_preview, defs)
            if match:
                key = (match.event, match.matcher,
                      match.messages[0] if match.messages else match.command[:60])
            else:
                key = ("unattributed", tool.name, signature(tool.output_preview))
            bucket = buckets.setdefault(key, {"event": key[0], "matcher": key[1],
                                              "message": key[2], "n": 0, "sids": set()})
            bucket["n"] += 1
            bucket["sids"].add(entry.session.sid)
    out = [{"event": b["event"], "matcher": b["matcher"], "message": b["message"],
           "n": b["n"], "sessions": len(b["sids"])} for b in buckets.values()]
    out.sort(key=lambda r: (-r["n"], r["event"], r["matcher"]))
    return out


def _all_deny_rules(corpus: Corpus) -> list[tuple[str, str]]:
    """Deny-rule ``(tool, pattern)`` pairs from every reachable source, deduplicated."""
    seen: set[tuple[str, str]] = set()
    for source in _config_sources(corpus):
        seen.update(sj.deny_rules(sj.read_settings(source.root)))
    return sorted(seen)


def _tool_command_text(tool: ToolCall) -> str:
    """Best-effort extraction of the value a deny pattern is meant to match."""
    try:
        parsed = json.loads(tool.input_preview)
    except (ValueError, TypeError):
        return tool.input_preview
    if isinstance(parsed, dict):
        for key in ("command", "file_path", "pattern", "url"):
            if key in parsed:
                return str(parsed[key])
    return tool.input_preview


def _match_deny(tool: ToolCall, rules: list[tuple[str, str]]) -> tuple[str, bool]:
    """Best deny-rule match for one failed call.

    Returns ``(pattern, confirmed)``. ``confirmed`` means the call's own input matched the
    rule's glob, not just its tool name — an unconfirmed match is reported as such rather than
    presented as the cause, since a tool can carry several deny rules for different patterns.
    """
    text = _tool_command_text(tool)
    same_tool = [pattern for name, pattern in rules if name == tool.name]
    for pattern in same_tool:
        if fnmatch.fnmatch(text, pattern):
            return (pattern, True)
    return (same_tool[0], False) if same_tool else ("", False)


def deny_rows(corpus: Corpus, scope: Filter) -> list[Row]:
    """``user-rejected``/``permission-denied``/``policy-denied`` failures, attributed to a
    ``permissions.deny`` rule where the call's own input confirms it, or flagged as
    unconfirmed/unattributed otherwise. Never edits ``settings.json`` — read-only, like every
    join in this module."""
    rules = _all_deny_rules(corpus)
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in scope.apply(corpus):
        for tool in entry.session.tools:
            # policy-denied covers the harness's own "permission to use X has been denied"
            # wording, which is exactly the shape a permissions.deny rule produces.
            if tool.err_class not in ("user-rejected", "permission-denied", "policy-denied"):
                continue
            pattern, confirmed = _match_deny(tool, rules)
            key = (tool.err_class, tool.name, pattern)
            bucket = buckets.setdefault(key, {"class": key[0], "tool": key[1],
                                              "pattern": key[2], "confirmed": confirmed,
                                              "n": 0, "sids": set()})
            bucket["n"] += 1
            bucket["sids"].add(entry.session.sid)
    out = [{"class": b["class"], "tool": b["tool"], "pattern": b["pattern"],
           "confirmed": b["confirmed"], "n": b["n"], "sessions": len(b["sids"])}
          for b in buckets.values()]
    out.sort(key=lambda r: (-r["n"], r["class"], r["tool"]))
    return out


# --- forensics ------------------------------------------------------------------------------

def forensics_findings(entry: Loaded) -> list[Row]:
    """One session's anomalies, most frequent first, each with its prevention hint."""
    ordered = sorted(entry.anomalies, key=lambda a: -a.count)
    return [{"kind": a.kind, "detail": a.detail, "count": a.count, "lines": a.lines,
             "hint": ANOMALY_HINTS.get(a.kind, "")} for a in ordered]


def forensics_timeline(entry: Loaded) -> list[Row]:
    """Only the timeline lines a finding actually cites — line-anchored, not the full session."""
    wanted = {line for a in entry.anomalies for line in a.lines}
    return [r for r in timeline_rows(entry) if r["line"] in wanted]


def forensics_health(entry: Loaded) -> Row:
    """Totals for the 'what went right' counterweight: a forensics report is not pure negativity."""
    tools = entry.session.tools
    failed = sum(1 for t in tools if is_failure(t))
    return {"tool_calls": len(tools), "failed": failed, "succeeded": len(tools) - failed}
