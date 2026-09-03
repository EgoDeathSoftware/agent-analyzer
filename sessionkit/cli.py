"""Command-line entry point for sessionkit.

Every command parses the transcripts it needs on demand — there is no cache, no index and no
database to keep current. A full corpus parse is ~1.2 s, which is cheaper than the staleness
contract a cache would need; see ``sessionkit.corpus`` for the measurements.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

from sessionkit import __version__, corpus as corpus_mod, pricing, query, search
from sessionkit import sources as src_mod
from sessionkit.classify import classify_error
from sessionkit.corpus import Corpus
from sessionkit.render import (BUDGET_AGGREGATE_KB, BUDGET_EXCERPT_KB, BUDGET_INDEX_KB,
                               Report, human_cost)

_DURATION = re.compile(r"^(\d+)\s*([hdw])$", re.I)
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
_ABSOLUTE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")


def since_cutoff(value: str | None) -> str:
    """Convert a ``7d``/``12h``/``2w`` window, or an absolute ``YYYY-MM-DD[THH:MM[:SS]]`` date,
    into an ISO-8601 cutoff timestamp.

    Args:
        value: A relative duration, an absolute date/datetime, or ``None`` for no cutoff.

    Returns:
        An ISO timestamp string, or ``""`` when no cutoff applies.

    Raises:
        SystemExit: If neither form parses — a silently-ignored ``--since`` would make a partial
            report look complete.
    """
    if not value:
        return ""
    value = value.strip()
    match = _DURATION.match(value)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        cutoff = datetime.now(timezone.utc) - timedelta(**{_UNITS[unit]: amount})
        return cutoff.isoformat().replace("+00:00", "Z")
    if _ABSOLUTE.match(value):
        iso = value if "T" in value else f"{value}T00:00:00"
        try:
            dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        except ValueError:
            raise SystemExit(f"unrecognised --since value {value!r}; expected e.g. 7d, 12h, 2w, "
                             "or an absolute date/time like 2026-08-17 or 2026-08-17T14:30") from None
        return dt.isoformat().replace("+00:00", "Z")
    raise SystemExit(f"unrecognised --since value {value!r}; expected e.g. 7d, 12h, 2w, or an "
                     "absolute date/time like 2026-08-17 or 2026-08-17T14:30")


def _scope(args: argparse.Namespace) -> query.Filter:
    """Build the shared session filter from the common scope flags."""
    return query.Filter(
        cutoff=since_cutoff(getattr(args, "since", None)),
        project=getattr(args, "project", None) or "",
        source=getattr(args, "source", None) or "",
        state=getattr(args, "state", None) or "",
        subagents=getattr(args, "subagents", "include"),
        label_contains=getattr(args, "label_contains", None) or "",
    )


def cmd_doctor(corpus: Corpus, args: argparse.Namespace) -> str:
    """Report source reachability and corpus totals."""
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(sessionkit=__version__)

    sources = query.source_rows(corpus)
    retention = query.retention_days(corpus)
    report.section("Sources")
    report.table(
        ["id", "kind", "location", "reachable", "retention_days", "root", "note"],
        [[r["id"], r["kind"], r["location"], "yes" if r["reachable"] else "NO",
          retention.get(r["id"]) if retention.get(r["id"]) is not None else "-",
          r["root"], r["note"]] for r in sources],
        key="sources",
    )
    unreachable = [r["id"] for r in sources if not r["reachable"]]
    if unreachable:
        report.text(f"Not visible from this process: {', '.join(unreachable)}. "
                    "Totals below cover only the reachable sources.")

    totals = query.totals(corpus)
    errors, calls = totals["errors"], totals["calls"]
    report.section("Corpus")
    report.table(
        ["metric", "value"],
        [["sessions", totals["n"] or 0], ["turns", totals["turns"] or 0],
         ["tool calls", calls], ["failed calls", f"{errors} ({_pct(errors, calls)})"],
         ["est. cost", human_cost(totals["cost"] or 0.0)],
         ["earliest", totals["first"] or "-"], ["latest", totals["last"] or "-"]],
        key="corpus",
    )
    if corpus.failed:
        report.text(f"{corpus.failed} transcript(s) could not be read and are excluded.")
    unknown = pricing.unknown_models()
    if unknown:
        report.text(f"Models with no pricing entry (billed at Sonnet default): "
                    f"{', '.join(unknown)}")
    return report.render()


def _index_cutoff(args: argparse.Namespace) -> str:
    """Resolve the ``--since`` window for ``sk index``.

    Layer 1 is meant for "what have I been doing", so it defaults to the last 3 days rather
    than the whole corpus. ``--today``/``--last`` narrow or widen that window; an explicit
    ``--since`` (shared with every other query command) still wins outright.
    """
    since, today, last = args.since, args.today, args.last
    if sum(bool(x) for x in (since, today, last is not None)) > 1:
        raise SystemExit("--since, --today, and --last are mutually exclusive")
    if since:
        return since
    if today:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if last is not None:
        if last < 1:
            raise SystemExit(f"--last must be a positive integer, got {last}")
        return f"{last}d"
    return "3d"


def cmd_index(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 1: one line per session, oldest first (most recent last)."""
    args.since = _index_cutoff(args)
    rows = query.index_rows(corpus, _scope(args))
    report = Report(args.json, args.budget_kb or BUDGET_INDEX_KB)
    report.meta(sessions=len(rows), subagents=args.subagents)
    headers = ["sid", "project", "ended", "turns", "cost", "state", "model", "label",
               "parent_sid", "agent_type"]
    show_lineage = args.subagents in ("include", "only")
    table_rows = [[r["sid"][:8], r["project_key"], (r["ended_at"] or "")[:16], r["turns"],
                   f"{r['cost_usd']:.2f}", r["end_state"], _short_model(r["model"]),
                   r["label"] or "",
                   r["parent_sid"][:8] if r["parent_sid"] else "-",
                   r["agent_type"] or "-"] for r in rows]
    # JSON callers always get lineage; the text table only spends columns on it when
    # subagents are in scope, so a plain `sk index` stays compact.
    if args.json or show_lineage:
        report.table(headers, table_rows, key="sessions")
    else:
        report.table(headers[:-2], [row[:-2] for row in table_rows], key="sessions")
    return report.render()


def cmd_errors(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 2: cluster every failed tool call across the fleet.

    This is the skill-facing entry point for ``error-patterns``: it answers "what fails most,
    and what is the fix" without any transcript reaching the caller's context.
    """
    scope = _scope(args)
    rows = query.clusters(corpus, scope, args.group_by)
    total = sum(r["n"] for r in rows)
    calls = query.tool_call_count(corpus, scope)

    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(failures=total, tool_calls=calls, failure_rate=_pct(total, calls),
                grouped_by=args.group_by)
    report.section(f"Failures by {args.group_by}")
    report.table(
        ["bucket", "count", "share", "sessions", "fix", "exemplar"],
        [[r["bucket"] or "-", r["n"], _pct(r["n"], total), r["sessions"],
          classify_error(r["exemplar"])[1], r["exemplar"] or ""] for r in rows],
        key="clusters",
    )
    if args.group_by == "class" and rows:
        report.section("Reading this")
        report.text(_headline(corpus, scope, rows, total))
    return report.render()


def cmd_commands(corpus: Corpus, args: argparse.Namespace) -> str:
    """Every tool call in scope: what an agent (or the fleet) actually ran.

    No UI equivalent exists — the Tools tab shows calls one at a time and cannot roll them up or
    mark which failed.
    """
    scope = _scope(args)
    agent_type = getattr(args, "agent_type", "") or ""
    rows = query.command_rows(corpus, scope, args.group_by, agent_type=agent_type)

    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB, full=args.full)
    report.meta(calls=sum(r["n"] for r in rows), grouped_by=args.group_by,
               agent_type=agent_type or "any")
    if args.group_by == "agent" and not rows:
        if agent_type:
            report.text(f"No subagent transcripts of type {agent_type!r} in scope.")
        elif not query.has_subagents(corpus, scope):
            report.text("No subagent transcripts in scope — nothing to attribute to an agent.")
    report.section(f"Commands by {args.group_by}")
    report.table(
        ["bucket", "tool", "count", "sessions", "errors", "avg_ms", "agent_type", "exemplar"],
        [[r["bucket"] or "-", r["tool"], r["n"], r["sessions"], r["errors"],
          r["avg_ms"] if r["avg_ms"] is not None else "-", r["agent_type"] or "-",
          r["exemplar"]] for r in rows],
        key="commands",
    )
    return report.render()


def cmd_hooks(corpus: Corpus, args: argparse.Namespace) -> str:
    """Join hook-block and deny-rule failures against ``settings.json``.

    Read-only: attributes failures to the rule that caused them so ``error-patterns`` can
    justify a relocation with a real count. Never proposes or edits the config itself.
    """
    scope = _scope(args)
    hooks = query.hook_rows(corpus, scope)
    denies = query.deny_rows(corpus, scope)

    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(hook_block_failures=sum(r["n"] for r in hooks),
               deny_failures=sum(r["n"] for r in denies))
    report.section("Hook blocks")
    report.table(
        ["event", "matcher", "message", "count", "sessions"],
        [[r["event"], r["matcher"], r["message"], r["n"], r["sessions"]] for r in hooks],
        key="hooks",
    )
    report.section("Denials (user-rejected / permission-denied / policy-denied)")
    report.table(
        ["class", "tool", "pattern", "confirmed", "count", "sessions"],
        [[r["class"], r["tool"], r["pattern"] or "-", "yes" if r["confirmed"] else "no",
          r["n"], r["sessions"]] for r in denies],
        key="denies",
    )
    return report.render()


def cmd_forensics(corpus: Corpus, args: argparse.Namespace) -> str:
    """Why one session went wrong: findings, a line-anchored timeline, and what went right.

    Deterministic by design — findings and hints are looked up from the detectors and
    ``classify.ANOMALY_HINTS``, not generated, so this is a report, not a narrative.
    """
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")

    findings = query.forensics_findings(entry)
    health = query.forensics_health(entry)

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB)
    report.meta(sid=entry.session.sid, project=entry.project_key,
               state=entry.session.end_state, findings=len(findings))
    if entry.session.end_reason:
        report.text(f"End reason: {entry.session.end_reason}")

    report.section("Findings")
    report.table(
        ["kind", "detail", "count", "lines", "prevention"],
        [[f["kind"], f["detail"], f["count"], ",".join(str(n) for n in f["lines"]), f["hint"]]
         for f in findings],
        key="findings",
    )
    report.section("Timeline (finding-anchored)")
    report.table(
        ["line", "kind", "what", "detail"],
        [[r["line"], r["kind"], r["name"], r["detail"] or ""]
         for r in query.forensics_timeline(entry)],
        key="timeline",
    )
    report.section("Health")
    report.table(
        ["metric", "value"],
        [["tool calls", health["tool_calls"]], ["succeeded", health["succeeded"]],
         ["failed", health["failed"]],
         ["success rate", _pct(health["succeeded"], health["tool_calls"])]],
        key="health",
    )
    return report.render()


def cmd_children(corpus: Corpus, args: argparse.Namespace) -> str:
    """Every Agent dispatch from one session, resolved to its child sid, state and cost.

    Collapses what FIRSTRUN.md §2 needed ten-plus `sk` calls and a label-text guess to find.
    """
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    rows = query.children_rows(entry, corpus)
    unresolved = sum(1 for r in rows if not r["resolved"])

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB)
    report.meta(sid=entry.session.sid, dispatches=len(rows), unresolved=unresolved)
    if unresolved:
        report.text(f"{unresolved} dispatch(es) have no task-notification match in this "
                    "transcript — reported as unresolved rather than guessed by order.")
    report.section("Children")
    report.table(
        ["line", "child_sid", "resolved", "agent_type", "state", "cost", "project", "dispatch"],
        [[r["line"], r["child_sid"][:8] if r["child_sid"] else "-",
          "yes" if r["resolved"] else "no", r["agent_type"] or "-", r["state"] or "-",
          f"{r['cost_usd']:.2f}", r["project"] or "-", r["description"]] for r in rows],
        key="children",
    )
    return report.render()


def _cost_scope(args: argparse.Namespace) -> query.Filter:
    """Like `_scope`, but `cost` overloads `--subagents` as its own comparison toggle (a bool,
    not the shared include/exclude/only scope choice every other command uses), so this builds
    the Filter directly instead of reading that flag — a real name collision, resolved locally
    rather than by teaching `_scope` to type-sniff every command's `--subagents`."""
    return query.Filter(
        cutoff=since_cutoff(getattr(args, "since", None)),
        project=getattr(args, "project", None) or "",
        source=getattr(args, "source", None) or "",
    )


def cmd_cost(corpus: Corpus, args: argparse.Namespace) -> str:
    """Token and dollar totals: corpus-wide, or narrowed to one session with --bloat/--subagents
    detail (layer 2/3, PLAN.md §7 Phase 5)."""
    scope = _cost_scope(args)
    if args.sid:
        return _cmd_cost_session(corpus, args, scope)
    return _cmd_cost_corpus(corpus, args, scope)


def _cmd_cost_corpus(corpus: Corpus, args: argparse.Namespace, scope: query.Filter) -> str:
    """Fleet-wide cost rollup, one row per session.

    Includes subagent sessions by default (Filter's own default), so `sessions` here can exceed
    `sk index`'s count and the --subagents footer's parent-only total — meta and the table's
    `kind` column state that split explicitly rather than leaving the discrepancy unexplained."""
    rows = scope.apply(corpus)
    total = sum(e.session.cost_usd for e in rows)
    top_level = sum(1 for e in rows if not e.session.is_subagent)
    subagent = sum(1 for e in rows if e.session.is_subagent)
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(sessions=len(rows), top_level=top_level, subagent=subagent,
               total_cost=human_cost(total))
    report.section("Cost by session")
    report.table(
        ["sid", "kind", "project", "model", "cost", "tok_in", "tok_out"],
        [[e.session.sid[:8], "subagent" if e.session.is_subagent else "top-level",
          e.project_key, _short_model(e.session.model),
          f"{e.session.cost_usd:.2f}", e.session.tok_in, e.session.tok_out]
         for e in sorted(rows, key=lambda e: -e.session.cost_usd)],
        key="sessions",
    )
    if args.bloat:
        _cost_bloat_section(corpus, scope, report)
    if args.subagents:
        _cost_subagents_section(corpus, scope, report)
    return report.render()


def _cmd_cost_session(corpus: Corpus, args: argparse.Namespace, scope: query.Filter) -> str:
    """One session's totals, tool breakdown, and the .claude.json cross-check."""
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    summary = query.session_cost_summary(entry)

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB)
    report.meta(sid=entry.session.sid, project=entry.project_key, model=summary["model"],
               cost=human_cost(summary["cost_usd"]))
    report.section("Totals")
    report.table(
        ["metric", "value"],
        [["total cost", human_cost(summary["cost_usd"])],
         ["tool cost", human_cost(summary["tool_cost"])],
         ["conversation cost", human_cost(summary["conversation_cost"])],
         ["input tokens", summary["tok_in"]], ["output tokens", summary["tok_out"]],
         ["cache read tokens", summary["tok_cache_read"]],
         ["cache create tokens", summary["tok_cache_create"]]],
        key="totals",
    )
    report.section("Cost by tool")
    report.table(
        ["tool", "calls", "cost"],
        [[r["tool"], r["n"], f"{r['cost_usd']:.4f}"] for r in query.tool_cost_rows(entry)],
        key="by_tool",
    )
    check = query.claude_json_cost_check(corpus, entry)
    if check is not None:
        report.text(f"~/.claude.json:lastModelUsage for this project's last session: "
                    f"{human_cost(check['claude_json_cost'])} (sk: "
                    f"{human_cost(check['sk_cost'])}, delta {check['delta']:+.4f}) — spot "
                    "check only, covers the last session per project.")
    if args.bloat:
        _cost_bloat_section(Corpus(sessions=[entry]), query.Filter(), report)
    if args.subagents:
        _cost_subagents_table(query.children_rows(entry, corpus), report)
    return report.render()


def _cost_bloat_section(corpus: Corpus, scope: query.Filter, report: Report) -> None:
    """Attach --bloat findings: oversized results, repeat reads, unbounded Bash output, and the
    cache-read/create ratio (NOTES.md §2.1). Reused for both corpus- and session-scoped `sk
    cost` — the caller passes a one-session Corpus for the latter."""
    report.section("Bloat: oversized tool results (avg > 10 KB)")
    report.table(
        ["tool", "calls", "avg_bytes", "max_bytes", "total_bytes"],
        [[r["tool"], r["n"], int(r["avg"]), r["max"], r["total"]]
         for r in query.oversized_tool_rows(corpus, scope)],
        key="oversized",
    )
    report.section("Bloat: repeat reads")
    report.table(
        ["sid", "path", "reads", "wasted_bytes"],
        [[r["sid"][:8], r["path"], r["reads"], r["wasted_bytes"]]
         for r in query.repeat_read_rows(corpus, scope)],
        key="repeat_reads",
    )
    report.section("Bloat: unbounded Bash output (> 10 KB)")
    report.table(
        ["sid", "line", "bytes", "command"],
        [[r["sid"][:8], r["line"], r["bytes"], r["input"]]
         for r in query.unbounded_bash_rows(corpus, scope)],
        key="unbounded_bash",
    )
    notices = query.truncation_notice_count(corpus, scope)
    ratio = query.cache_ratio(corpus, scope)
    report.section("Bloat: cache and truncation")
    report.table(
        ["metric", "value"],
        [["read_truncation_notice count", notices],
         ["cache_read tokens", ratio["cache_read"]],
         ["cache_create tokens", ratio["cache_create"]],
         ["read/create ratio", f"{ratio['ratio']:.2f}" if ratio["ratio"] is not None else "n/a"]],
        key="cache",
    )


def _cost_subagents_section(corpus: Corpus, scope: query.Filter, report: Report) -> None:
    """Attach --subagents fleet-wide dispatch rows, with sample size stated up front."""
    summary = query.subagent_cost_summary(corpus, scope)
    report.section("Subagents")
    report.text(f"{summary['dispatches']} dispatch(es) in scope, {summary['resolved']} "
               f"resolved ({summary['sunk']} sunk, {summary['wasted']} flagged wasted).")
    if summary["resolved"] < 3:
        report.text("Fewer than 3 resolved dispatches — too few to draw a conclusion from.")
    report.table(
        ["parent_sid", "child_sid", "agent_type", "state", "cost", "sunk", "wasted"],
        [[r["parent_sid"][:8], r["child_sid"][:8] if r["child_sid"] else "-",
          r["agent_type"] or "-", r["state"] or "-", f"{r['cost_usd']:.2f}",
          "yes" if r["sunk"] else "no", "yes" if r["wasted"] else "no"]
         for r in query.subagent_dispatch_rows(corpus, scope)],
        key="dispatches",
    )
    report.text(f"Child cost total: {human_cost(summary['child_cost_total'])}; parent cost "
               f"total: {human_cost(summary['parent_cost_total'])}.")


def _cost_subagents_table(rows: list[query.Row], report: Report) -> None:
    """This session's own dispatches, annotated sunk/wasted (session-scoped --subagents)."""
    annotated = [query.annotate_dispatch(r) for r in rows]
    report.section("Subagents (this session's dispatches)")
    report.table(
        ["child_sid", "agent_type", "state", "cost", "sunk", "wasted"],
        [[r["child_sid"][:8] if r["child_sid"] else "-", r["agent_type"] or "-",
          r["state"] or "-", f"{r['cost_usd']:.2f}", "yes" if r["sunk"] else "no",
          "yes" if r["wasted"] else "no"] for r in annotated],
        key="dispatches",
    )


def cmd_tail(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 3: the last N turns of one session, plus its tail signal.

    Judgment about `done vs unfinished` lives in the `unfinished-work` skill; this command
    surfaces the material and one deterministic classification, nothing more.
    """
    if getattr(args, "all", False):
        return _cmd_tail_all(corpus, args)
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    signal = query.tail_signal(entry, n=args.n)

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB, full=args.full)
    report.meta(sid=entry.session.sid, project=entry.project_key,
                state=entry.session.end_state, tail_signal=signal,
                n=args.n, turns=entry.session.turns)
    report.section(f"Tail (last {args.n})")
    report.table(["line", "role", "chars", "preview"],
                 [[r["line"], r["role"], r["chars"], r["preview"]]
                  for r in query.tail_rows(entry, n=args.n, full=args.full,
                                           include_tools=not args.no_tools)],
                 key="tail")
    return report.render()


def _cmd_tail_all(corpus: Corpus, args: argparse.Namespace) -> str:
    """Corpus scan: every non-complete, non-live session, with its tail signal."""
    candidates = query.tail_candidates(corpus, _scope(args))
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB, full=args.full)
    report.meta(candidates=len(candidates), n=args.n)
    report.section("Candidates (non-complete, not currently running)")
    rows: list[list] = []
    for entry in candidates:
        signal = query.tail_signal(entry, n=args.n)
        last = entry.session.messages[-1] if entry.session.messages else None
        excerpt = ""
        if last is not None:
            tail_texts = query.tail_context(entry, n=1)
            excerpt = tail_texts.get(last.line, last.preview or "")
        rows.append([entry.session.sid[:8], entry.session.end_state, signal,
                     (entry.session.ended_at or "")[:16], excerpt])
    report.table(["sid", "state", "tail_signal", "ended", "tail_excerpt"], rows,
                 key="candidates")
    return report.render()


def cmd_files(corpus: Corpus, args: argparse.Namespace) -> str:
    """Files touched by one session, or rolled up across a project/corpus scope."""
    if args.sid:
        entry = query.find_session(corpus, args.sid)
        if entry is None:
            raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
        rows = query.file_rows(entry)
        note = ""
        if args.uncommitted:
            dirty, note = query.uncommitted_intersection(
                entry.session.cwd, [r["path"] for r in rows])
            rows = [r for r in rows if r["path"] in dirty]
        report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
        report.meta(sid=entry.session.sid, project=entry.project_key,
                    cwd=entry.session.cwd, paths=len(rows))
        if args.uncommitted:
            report.meta(uncommitted=len(rows))
        report.section("Files")
        report.table(
            ["path", "reads", "writes", "edits", "first_line", "last_line"],
            [[r["path"], r["reads"], r["writes"], r["edits"],
              r["first_line"], r["last_line"]] for r in rows],
            key="files",
        )
        if note:
            report.text(f"git join: {note}")
        return report.render()

    rows = query.file_project_rows(corpus, _scope(args))
    notes: list[str] = []
    if args.uncommitted:
        cwd_of: dict[str, str] = {}
        for r in rows:
            sid = r["exemplar_sid"]
            if sid not in cwd_of:
                exemplar = query.find_session(corpus, sid)
                cwd_of[sid] = exemplar.session.cwd if exemplar else ""
        by_cwd: dict[str, list[str]] = {}
        for r in rows:
            by_cwd.setdefault(cwd_of[r["exemplar_sid"]], []).append(r["path"])
        dirty_all: set[str] = set()
        for cwd, paths in by_cwd.items():
            if not cwd:
                continue
            dirty, note = query.uncommitted_intersection(cwd, paths)
            dirty_all |= dirty
            if note:
                notes.append(note)
        rows = [r for r in rows if r["path"] in dirty_all]
    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB)
    report.meta(paths=len(rows), project=args.project or "any")
    if args.uncommitted:
        report.meta(uncommitted=len(rows))
    report.section("Files across scope")
    report.table(
        ["path", "sessions", "reads", "writes", "edits", "exemplar_sid"],
        [[r["path"], r["sessions"], r["reads"], r["writes"], r["edits"],
          r["exemplar_sid"][:8]] for r in rows],
        key="files",
    )
    if notes:
        report.text(f"git join: {'; '.join(sorted(set(notes)))}")
    return report.render()


def cmd_search(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 2: full-text search across every reachable transcript's raw lines.

    Matches raw JSONL text, not any in-memory preview, so a query that only appears in a
    spilled tool result's full text still hits (PLAN.md §3.2.1) — the transcript's own
    `toolUseResult` copy sits on the same line as the truncated stub. No index: only files
    that already matched get parsed, for scope filtering and a line-anchored excerpt.
    """
    if args.per_session < 0 or args.limit < 0:
        raise SystemExit("--per-session and --limit must be >= 0 (0 means unlimited)")
    pattern = search.compile_query(args.query, regex=args.regex,
                                   case_sensitive=args.case_sensitive)
    scope = _scope(args)
    rows, degraded, total = search.search_rows(
        corpus.sources, scope, pattern, context=args.context,
        per_session=args.per_session, limit=args.limit)

    report = Report(args.json, args.budget_kb or BUDGET_AGGREGATE_KB, full=args.full)
    report.meta(query=args.query, regex=args.regex, hits=total, shown=len(rows))
    if total > len(rows):
        report.text(f"{total - len(rows)} additional match(es) exist beyond --limit/"
                    "--per-session and are not shown; raise --limit or --per-session to see "
                    "them.")
    if degraded:
        report.text(f"{degraded} match(es) found only in a spilled tool-results/ file whose "
                    "transcript could not be resolved and are omitted from the count above "
                    "(checked regardless of --project/--since scope, since an unresolvable "
                    "spill has no parsed session to filter against) — the session's own copy "
                    "has aged out from under the spill file.")
    report.section("Matches")
    report.table(
        ["sid", "project", "line", "kind", "excerpt"],
        [[r["sid"][:8], r["project"], r["line"], r["kind"], r["excerpt"]] for r in rows],
        key="matches",
    )
    return report.render()


def _headline(corpus: Corpus, scope: query.Filter, rows: list[query.Row], total: int) -> str:
    """Summarise the clusters, ranking by breadth as well as raw count.

    Count alone is a poor guide to what to fix: ``exit-code`` is usually the largest class but
    is mostly genuine command failures, while a single self-inflicted signature can be smaller
    yet touch far more sessions. Both are reported, plus the dominant single signature — which
    is the level a fix actually lands at.
    """
    top = rows[0]
    widest = max(rows, key=lambda r: r["sessions"])
    sig = query.dominant_signature(corpus, scope)
    total_sessions = query.session_count(corpus, scope)

    parts = [f"Largest class is {top['bucket']!r} ({top['n']}/{total}, "
             f"{_pct(top['n'], total)}) across {top['sessions']} session(s)."]
    if widest["bucket"] != top["bucket"]:
        parts.append(f"Widest reach is {widest['bucket']!r}, touching "
                     f"{widest['sessions']}/{total_sessions} sessions — a smaller class that "
                     f"affects more of the fleet is usually the better fix.")
    if sig:
        parts.append(f"Dominant single signature ({sig['n']} failures across "
                     f"{sig['sessions']} sessions): {sig['bucket'][:90]}")
    return " ".join(parts)


def cmd_show(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 3: a surgical excerpt of one session."""
    entry = query.find_session(corpus, args.sid)
    if entry is None:
        raise SystemExit(f"no session matching {args.sid!r} (try `sk index`)")
    session = entry.session

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB, full=args.full)
    report.meta(sid=session.sid, project=entry.project_key, model=session.model,
                state=session.end_state, turns=session.turns,
                cost=human_cost(session.cost_usd), path=session.path)
    if session.end_reason:
        report.text(f"End reason: {session.end_reason}")

    handlers = {"timeline": _show_timeline, "messages": _show_messages,
                "tools": _show_tools, "errors": _show_errors}
    handlers.get(args.mode, _show_summary)(entry, args, report)
    return report.render()


def _show_summary(entry: corpus_mod.Loaded, _args: argparse.Namespace,
                  report: Report) -> None:
    """Anomalies plus tool-usage totals."""
    report.section("Anomalies")
    report.table(["kind", "detail", "count", "lines"],
                 [[a["kind"], a["detail"], a["count"], a["lines"]]
                  for a in query.anomaly_rows(entry)],
                 key="anomalies")
    report.section("Tools")
    report.table(["tool", "calls", "errors"],
                 [[t["name"], t["n"], t["errs"]] for t in query.tool_totals(entry)],
                 key="tools")
    report.section("Skills")
    report.table(["skill", "calls", "errors"],
                 [[s["skill"], s["n"], s["errs"]] for s in query.skill_rows(entry)],
                 key="skills")


def _show_timeline(entry: corpus_mod.Loaded, _args: argparse.Namespace,
                   report: Report) -> None:
    """Interleaved messages, tool calls and system events in line order."""
    report.section("Timeline")
    report.table(["line", "kind", "what", "detail"],
                 [[r["line"], r["kind"], r["name"], r["detail"] or ""]
                  for r in query.timeline_rows(entry)],
                 key="timeline")


def _show_messages(entry: corpus_mod.Loaded, args: argparse.Namespace,
                   report: Report) -> None:
    """A line-numbered range of messages."""
    lo, hi = _range(args.range)
    report.section(f"Messages {lo}:{hi}")
    report.table(["line", "role", "chars", "preview"],
                 [[r["line"], r["role"], r["text_len"], r["preview"]]
                  for r in query.message_rows(entry, lo, hi, full=args.full)],
                 key="messages")


def _show_tools(entry: corpus_mod.Loaded, args: argparse.Namespace, report: Report) -> None:
    """Every tool call in the session."""
    report.section("Tool calls")
    report.table(["line", "tool", "ms", "err", "input"],
                 [[r["line"], r["name"], r["dur_ms"], r["err_class"] or "",
                   r["input_preview"]] for r in query.tool_rows(entry, full=args.full)],
                 key="tools")


def _show_errors(entry: corpus_mod.Loaded, args: argparse.Namespace, report: Report) -> None:
    """Only the failing tool calls, with their fix hints."""
    report.section("Errors")
    report.table(["line", "tool", "class", "fix", "detail"],
                 [[r["line"], r["name"], r["err_class"],
                   classify_error(r["output_preview"])[1],
                   r["output_preview"] or ""] for r in query.error_rows(entry, full=args.full)],
                 key="errors")


def _range(value: str | None) -> tuple[int, int]:
    """Parse an ``A:B`` line range."""
    if not value:
        return (1, 10_000)
    parts = value.split(":", 1)
    try:
        lo = int(parts[0]) if parts[0] else 1
        hi = int(parts[1]) if len(parts) > 1 and parts[1] else 10_000
    except ValueError:
        raise SystemExit(f"bad --range {value!r}; expected A:B") from None
    return (lo, hi)


def _pct(part: int, whole: int) -> str:
    """Format a percentage, tolerating a zero denominator."""
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def _short_model(model: str) -> str:
    """Abbreviate a model id for table display."""
    return pricing.normalise(model).replace("claude-", "") or "-"


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for every subcommand."""
    # The global flags live on a parent parser so they are accepted either before or after the
    # subcommand: `sk --json index` and `sk index --json` both work. Skills compose these
    # invocations as strings, and a flag that only parses in one position is a trap.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit JSON instead of text")
    common.add_argument("--budget-kb", type=float, default=0.0,
                        help="cap output size; excess rows are dropped with a notice")

    parser = argparse.ArgumentParser(prog="sk", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    # Every subparser below also carries these flags, so they parse after the subcommand too
    # (`sk index --json`). Giving them `common` again there would work for that position but
    # break the "before" position: argparse merges a subparser's own defaults into the top-level
    # namespace after parsing, so an unset `--json` on the subparser (default False) would
    # silently clobber a `--json` already set at the top level (`sk --json index`). SUPPRESS
    # means "absent unless explicitly passed here", so the merge only ever overrides when the
    # flag actually appears after the subcommand.
    common_sub = argparse.ArgumentParser(add_help=False)
    common_sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help="emit JSON instead of text")
    common_sub.add_argument("--budget-kb", type=float, default=argparse.SUPPRESS,
                            help="cap output size; excess rows are dropped with a notice")

    sub.add_parser("doctor", parents=[common_sub],
                   help="source reachability and corpus totals")

    # Filter flags shared by every query command, so `--since`/`--project`/`--source` mean the
    # same thing everywhere. `--subagents` is deliberately NOT here: its default differs per
    # command (§7 Phase 2), and a subparser's set_defaults() would otherwise mutate the shared
    # Action object's default for every other subparser using it too — each command adds its
    # own below.
    scoped = argparse.ArgumentParser(add_help=False)
    for arg in ("--since", "--project", "--source"):
        scoped.add_argument(arg)

    def _subagents_arg(sub_parser: argparse.ArgumentParser, default: str) -> None:
        sub_parser.add_argument("--subagents", choices=["include", "exclude", "only"],
                                default=default, help=f"subagent transcripts (default: {default})")

    p_index = sub.add_parser("index", parents=[common_sub, scoped],
                             help="one line per session (layer 1)")
    p_index.add_argument("--state", help="filter by end_state, e.g. interrupted-tool")
    p_index.add_argument("--label-contains",
                         help="only sessions whose title/label contains this text "
                              "(case-insensitive)")
    p_index.add_argument("--today", action="store_true",
                         help="only sessions ended today (UTC); default is the last 3 days")
    p_index.add_argument("--last", type=int, metavar="N",
                         help="only sessions ended in the last N days; default is 3")
    _subagents_arg(p_index, "exclude")

    p_show = sub.add_parser("show", parents=[common_sub],
                            help="excerpt one session (layer 3)")
    p_show.add_argument("sid", help="session id or unique prefix")
    p_show.add_argument("--mode", default="summary",
                        choices=["summary", "timeline", "messages", "tools", "errors"])
    p_show.add_argument("--range", help="line range for --mode messages, e.g. 40:80")
    p_show.add_argument("--full", action="store_true",
                        help="re-read tool/message text from source for full fidelity, past "
                             "the in-memory preview cap (JSON never truncates a cell either way)")

    p_err = sub.add_parser("errors", parents=[common_sub, scoped],
                           help="cluster tool failures fleet-wide (layer 2)")
    p_err.add_argument("--group-by", choices=["class", "tool", "signature", "session"],
                       default="class")
    _subagents_arg(p_err, "exclude")

    p_cmd = sub.add_parser("commands", parents=[common_sub, scoped],
                           help="review every tool call: what actually ran")
    p_cmd.add_argument("--group-by", choices=["command", "tool", "session", "agent"],
                       default="command")
    p_cmd.add_argument("--agent-type", help="scope to subagents of this type, e.g. Explore")
    p_cmd.add_argument("--full", action="store_true",
                       help="do not truncate cell text in text mode (JSON never truncates)")
    # Subagent visibility is the point of this command, unlike index/errors' fleet dashboards.
    _subagents_arg(p_cmd, "include")

    p_hooks = sub.add_parser("hooks", parents=[common_sub, scoped],
                             help="join hook-block/deny failures against settings.json")
    _subagents_arg(p_hooks, "exclude")

    p_forensics = sub.add_parser("forensics", parents=[common_sub],
                                 help="why one session went wrong (layer 3)")
    p_forensics.add_argument("sid", help="session id or unique prefix")

    p_children = sub.add_parser("children", parents=[common_sub],
                                 help="Agent dispatches from one session, resolved to child sid")
    p_children.add_argument("sid", help="session id or unique prefix")

    p_cost = sub.add_parser("cost", parents=[common_sub, scoped],
                            help="token/dollar totals, fleet-wide or for one session "
                                 "(layer 2/3)")
    p_cost.add_argument("sid", nargs="?", default=None,
                        help="session id or unique prefix; omit for a corpus-wide rollup")
    p_cost.add_argument("--bloat", action="store_true",
                        help="oversized results, repeat reads, unbounded Bash output, "
                             "cache ratio")
    p_cost.add_argument("--subagents", action="store_true",
                        help="compare subagent dispatch cost against the parent, flagging "
                             "sunk and wasted dispatches")
    # Note deliberately no `_subagents_arg(p_cost, ...)` call: `cost`'s own `--subagents` is
    # the comparison toggle from PLAN.md §7 Phase 5, not the shared include/exclude/only scope
    # flag every other command uses — the two can't coexist as the same flag name. This is
    # exactly why `cmd_cost` uses the local `_cost_scope` helper above instead of the shared
    # `_scope`: `_scope` reads `getattr(args, "subagents", "include")` as a scope choice, which
    # would misread `cost`'s boolean flag. `_scope` itself is untouched — every other command
    # keeps working exactly as before.

    p_tail = sub.add_parser("tail", parents=[common_sub, scoped],
                            help="last N turns of one session, with a tail signal")
    group = p_tail.add_mutually_exclusive_group(required=True)
    group.add_argument("sid", nargs="?", default=None,
                       help="session id or unique prefix")
    group.add_argument("--all", action="store_true",
                       help="scan every non-complete session in scope (excludes live "
                            "sessions from `sessions/*.json`)")
    p_tail.add_argument("--n", type=int, default=6,
                        help="number of trailing turns to include (default: 6)")
    p_tail.add_argument("--no-tools", action="store_true",
                        help="only chat messages (user/assistant), no tool calls")
    p_tail.add_argument("--full", action="store_true",
                        help="re-read message text from source for full fidelity "
                             "(JSON never truncates a cell either way)")
    _subagents_arg(p_tail, "include")

    p_files = sub.add_parser("files", parents=[common_sub, scoped],
                             help="files a session (or scope) touched")
    p_files.add_argument("sid", nargs="?", default=None,
                         help="session id or unique prefix; omit for a project rollup")
    p_files.add_argument("--uncommitted", action="store_true",
                         help="intersect with `git status --porcelain` in the session's cwd")
    _subagents_arg(p_files, "include")

    p_search = sub.add_parser("search", parents=[common_sub, scoped],
                              help="full-text search across every transcript (layer 2)")
    p_search.add_argument("query", help="text to search for (or a pattern with --regex)")
    p_search.add_argument("--regex", action="store_true",
                          help="treat query as a regular expression")
    p_search.add_argument("--case-sensitive", action="store_true",
                          help="match case exactly instead of folding it")
    p_search.add_argument("--context", type=int, default=2,
                          help="lines of context before/after each hit, by transcript line "
                               "number, not turn count (default: 2)")
    p_search.add_argument("--per-session", type=int, default=1,
                          help="max hits shown per session, keeping the most recent match(es) "
                               "first; 0 for unlimited (default: 1)")
    p_search.add_argument("--limit", type=int, default=20,
                          help="max hit rows returned overall; 0 for unlimited (default: 20)")
    p_search.add_argument("--full", action="store_true",
                          help="do not truncate excerpt cells in text mode (JSON never "
                               "truncates)")
    _subagents_arg(p_search, "include")
    return parser


COMMANDS = {"doctor": cmd_doctor, "index": cmd_index, "show": cmd_show, "errors": cmd_errors,
             "commands": cmd_commands, "hooks": cmd_hooks, "forensics": cmd_forensics,
             "children": cmd_children, "cost": cmd_cost, "tail": cmd_tail, "files": cmd_files,
             "search": cmd_search}


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.error(f"unhandled command {args.command}")
    sys.stdout.write(handler(_corpus_for(args), args))
    return 0


def _corpus_for(args: argparse.Namespace) -> Corpus:
    """Parse only what the command needs.

    ``show`` wants one session, so it tries to find it by filename first. ``search`` needs no
    parsed sessions at all up front — it only needs the discovered ``sources`` list, since
    ``search.search_rows`` parses (via ``corpus.load_one``) only the files that already
    matched the raw-text scan. Every other command aggregates and needs everything. The fast
    paths are advisory — when ``show``'s cannot confirm a hit we fall back to a full parse
    rather than reporting the session missing.
    """
    if args.command == "show":
        entry = corpus_mod.load_session(args.sid)
        if entry is not None:
            return Corpus(sessions=[entry])
    if args.command == "search":
        return Corpus(sources=src_mod.discover())
    return corpus_mod.load()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
