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

from sessionkit import __version__, corpus as corpus_mod, pricing, query
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


def cmd_index(corpus: Corpus, args: argparse.Namespace) -> str:
    """Layer 1: one line per session."""
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

    report = Report(args.json, args.budget_kb or BUDGET_EXCERPT_KB)
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
                  for r in query.message_rows(entry, lo, hi)],
                 key="messages")


def _show_tools(entry: corpus_mod.Loaded, _args: argparse.Namespace, report: Report) -> None:
    """Every tool call in the session."""
    report.section("Tool calls")
    report.table(["line", "tool", "ms", "err", "input"],
                 [[r["line"], r["name"], r["dur_ms"], r["err_class"] or "",
                   r["input_preview"]] for r in query.tool_rows(entry)], key="tools")


def _show_errors(entry: corpus_mod.Loaded, _args: argparse.Namespace, report: Report) -> None:
    """Only the failing tool calls, with their fix hints."""
    report.section("Errors")
    report.table(["line", "tool", "class", "fix", "detail"],
                 [[r["line"], r["name"], r["err_class"],
                   classify_error(r["output_preview"])[1],
                   r["output_preview"] or ""] for r in query.error_rows(entry)],
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

    sub.add_parser("doctor", parents=[common],
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

    p_index = sub.add_parser("index", parents=[common, scoped],
                             help="one line per session (layer 1)")
    p_index.add_argument("--state", help="filter by end_state, e.g. interrupted-tool")
    p_index.add_argument("--label-contains",
                         help="only sessions whose title/label contains this text "
                              "(case-insensitive)")
    _subagents_arg(p_index, "exclude")

    p_show = sub.add_parser("show", parents=[common],
                            help="excerpt one session (layer 3)")
    p_show.add_argument("sid", help="session id or unique prefix")
    p_show.add_argument("--mode", default="summary",
                        choices=["summary", "timeline", "messages", "tools", "errors"])
    p_show.add_argument("--range", help="line range for --mode messages, e.g. 40:80")

    p_err = sub.add_parser("errors", parents=[common, scoped],
                           help="cluster tool failures fleet-wide (layer 2)")
    p_err.add_argument("--group-by", choices=["class", "tool", "signature", "session"],
                       default="class")
    _subagents_arg(p_err, "exclude")

    p_cmd = sub.add_parser("commands", parents=[common, scoped],
                           help="review every tool call: what actually ran")
    p_cmd.add_argument("--group-by", choices=["command", "tool", "session", "agent"],
                       default="command")
    p_cmd.add_argument("--agent-type", help="scope to subagents of this type, e.g. Explore")
    p_cmd.add_argument("--full", action="store_true",
                       help="do not truncate cell text in text mode (JSON never truncates)")
    # Subagent visibility is the point of this command, unlike index/errors' fleet dashboards.
    _subagents_arg(p_cmd, "include")

    p_hooks = sub.add_parser("hooks", parents=[common, scoped],
                             help="join hook-block/deny failures against settings.json")
    _subagents_arg(p_hooks, "exclude")

    p_forensics = sub.add_parser("forensics", parents=[common],
                                 help="why one session went wrong (layer 3)")
    p_forensics.add_argument("sid", help="session id or unique prefix")

    p_children = sub.add_parser("children", parents=[common],
                                help="Agent dispatches from one session, resolved to child sid")
    p_children.add_argument("sid", help="session id or unique prefix")
    return parser


COMMANDS = {"doctor": cmd_doctor, "index": cmd_index, "show": cmd_show, "errors": cmd_errors,
            "commands": cmd_commands, "hooks": cmd_hooks, "forensics": cmd_forensics,
            "children": cmd_children}


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

    ``show`` wants one session, so it tries to find it by filename first; every other command
    aggregates and needs everything. The fast path is advisory — when it cannot confirm a hit
    we fall back to a full parse rather than reporting the session missing.
    """
    if args.command == "show":
        entry = corpus_mod.load_session(args.sid)
        if entry is not None:
            return Corpus(sessions=[entry])
    return corpus_mod.load()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
