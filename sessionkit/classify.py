"""Error taxonomy, session end-state derivation, and anomaly detection.

The taxonomy is grounded in the live corpus rather than guessed: clustering the 208 error
results across 86 transcripts put ``hook-block`` first at 47 occurrences (23% of all failures),
ahead of generic exit codes and user rejections. Text that matches nothing lands in ``other``
**with its normalised prefix retained**, so the taxonomy grows from real misses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from sessionkit.parse import ParsedSession, ToolCall

#: All tuneable detector limits in one place rather than scattered as literals.
THRESHOLDS = {
    "repeat_tool": 3,      # identical (tool, input) calls in one session
    "file_thrash": 5,      # edits to the same path
    "read_loop": 4,        # reads of the same path
    "error_cascade": 3,    # consecutive failing tool calls
    "hook_pingpong": 2,    # hook blocks against the same tool
    "compaction_churn": 2, # compact_boundary events
    "stall_ms": 120_000,   # a single tool call taking longer than this
}

#: Ordered because several patterns overlap — a hook block also contains "exit".
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("hook-block", re.compile(r"PreToolUse:|PostToolUse:.*hook error|\bBLOCKED:", re.I),
     "Promote the rule into CLAUDE.md so the command is never emitted."),
    ("user-rejected", re.compile(r"user doesn't want to proceed|tool use was rejected", re.I),
     "Approach problem — the model proposed something the user declined."),
    # Distinct from user-rejected: no human saw this one, the harness blocked it.
    ("policy-denied",
     re.compile(r"denied by the Claude Code|auto mode classifier|"
                r"permission for this action was denied|"
                r"permission to use \w+.{0,200}?has been denied", re.I | re.S),
     "Blocked by permission policy — needs approval or a different approach."),
    # A malformed call is the model's own bug, not the environment's.
    ("input-validation",
     re.compile(r"InputValidationError|parameter `\w+` type is expected", re.I),
     "Malformed tool call — the model passed a wrong argument type."),
    ("file-too-large", re.compile(r"exceeds maximum allowed tokens", re.I),
     "Read with offset/limit, or use Grep instead of a full read."),
    ("read-truncated", re.compile(r"read_truncation|truncated to \d+ lines", re.I),
     "Narrow the read; the model only saw part of the file."),
    ("stale-read", re.compile(r"has been modified since read|file has changed since", re.I),
     "Re-read the file before editing; another writer changed it."),
    ("unread-file", re.compile(r"has not been read yet|read it first", re.I),
     "Read before Write/Edit — the tool requires it."),
    ("missing-tool", re.compile(r"command not found|: not found$|\bno such command\b", re.I),
     "Install the tool or drop the dependency on it."),
    # Must precede exit-code: a denied mkdir surfaces as "Exit code 1 ... Permission denied".
    ("permission-denied", re.compile(r"EACCES|permission denied|EPERM\b", re.I),
     "Write target is not writable by this user — pick a writable path."),
    ("not-a-repo", re.compile(r"not a git repository", re.I),
     "cwd assumption — the command ran outside a repo."),
    ("not-found",
     re.compile(r"File does not exist|Directory does not exist|no such file or directory|"
                r"\bENOENT\b", re.I),
     "Path assumption — verify the path before operating on it."),
    ("rate-limit", re.compile(r"rate limit|429\b|too many requests", re.I),
     "Back off, or route to an alternate provider."),
    ("api-error", re.compile(r"request failed with status code|\b5\d\d\b error", re.I),
     "Transient or misconfigured endpoint — check which."),
    ("mcp-error", re.compile(r"error in mcp__|mcp server .* (failed|error)", re.I),
     "MCP server health — check the server, not the prompt."),
    ("timeout", re.compile(r"timed out|timeout exceeded", re.I),
     "Raise the timeout or split the work."),
    ("exit-code", re.compile(r"^exit code \d+", re.I),
     "Genuine command failure — read the command output."),
]

#: Exit codes whose meaning is unambiguous. Checked *before* the body-text rules because the
#: code is the tool's own verdict about the call, while body text is often incidental — a
#: compound command's stray ``ls: No such file`` should not outrank the fact that the call
#: itself timed out.
_EXIT_CODES: dict[int, tuple[str, str]] = {
    124: ("timeout", "Command hit its timeout — raise it or split the work."),
    137: ("killed", "Killed (SIGKILL, usually OOM) — the work did not complete."),
    143: ("killed", "Terminated (SIGTERM) — the work did not complete."),
}
_EXIT_RE = re.compile(r"^exit code (\d+)\b", re.I)

_NORMALISE = [
    (re.compile(r"/[\w./\-]{6,}"), "<PATH>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


@dataclass
class Anomaly:
    """One detected problem in a session."""

    kind: str
    detail: str
    count: int
    lines: list[int]


#: Canned prevention text per anomaly kind, parallel to _RULES' fix hints for error classes.
#: This is what makes ``sk forensics`` end on a "prevention diff, not a narrative" (PLAN.md §7
#: Phase 2): the hint is looked up, not generated.
ANOMALY_HINTS: dict[str, str] = {
    "repeat-tool": "Same call retried with identical input — check why the prior result wasn't "
                   "used instead of re-running it.",
    "file-thrash": "Same file edited repeatedly — plan the full change before editing, or use "
                   "MultiEdit for a single pass.",
    "read-loop": "Same file re-read repeatedly — remember its contents instead of re-reading.",
    "error-cascade": "Consecutive failures — stop and diagnose the root cause instead of "
                     "retrying variants of the same call.",
    "hook-pingpong": "The same tool was blocked by a hook repeatedly — the rule belongs in "
                     "CLAUDE.md, where it teaches at generation time instead of blocking at "
                     "execution time.",
    "agent-kill": "A subagent was killed before returning — check the dispatch conditions or "
                  "the timeout that ended it.",
    "orphan-subagent": "A subagent was spawned and never returned — check whether it stalled "
                       "or the parent moved on without waiting.",
    "compaction-churn": "Context was compacted more than once — the session outgrew its "
                        "window; consider splitting the work or narrowing scope.",
    "stall": "A single tool call ran unusually long — check for a network hang or an "
             "unbounded operation.",
    "rejection-persist": "A call was retried after the user explicitly rejected it — respect "
                         "the rejection and ask before retrying, rather than trying again "
                         "unprompted.",
}


def classify_error(text: str) -> tuple[str, str]:
    """Classify an error result body.

    Args:
        text: The tool result text (already redacted and previewed).

    Returns:
        ``(class, fix_hint)``. Unmatched text returns ``("other", "")``.
    """
    body = (text or "").strip()
    if not body:
        return ("empty", "")
    match = _EXIT_RE.match(body)
    if match:
        known = _EXIT_CODES.get(int(match.group(1)))
        if known:
            return known
    for name, pattern, hint in _RULES:
        if pattern.search(body):
            return (name, hint)
    return ("other", "")


def signature(text: str, limit: int = 80) -> str:
    """Collapse an error body into a clusterable signature.

    Digits, hex and long paths are the parts that differ between otherwise-identical failures,
    so they are replaced with placeholders before truncation.
    """
    body = (text or "").strip()
    for pattern, replacement in _NORMALISE:
        body = pattern.sub(replacement, body)
    return body[:limit]


def annotate_errors(session: ParsedSession) -> None:
    """Fill in ``err_class``/``err_detail`` on every failed tool call, in place."""
    for call in session.tools:
        if call.err_class == "no-result":
            call.err_detail = "tool_use with no matching result"
            continue
        if not call.is_error:
            continue
        cls, _hint = classify_error(call.output_preview)
        call.err_class = cls
        call.err_detail = signature(call.output_preview)


def derive_end_state(session: ParsedSession) -> tuple[str, str]:
    """Decide how a session ended, and why.

    Returns:
        ``(end_state, reason)`` where end_state is one of ``complete``,
        ``interrupted-tool``, ``killed-agents``, ``error-cascade``, ``compacted-idle``,
        ``interrupted-user`` or ``unknown``.
    """
    orphans = [c for c in session.tools if c.err_class == "no-result"]
    if orphans:
        names = ", ".join(sorted({c.name for c in orphans})[:3])
        return ("interrupted-tool", f"{len(orphans)} unresolved tool call(s): {names}")
    if any(e.subtype == "agents_killed" for e in session.sysev):
        return ("killed-agents", "a subagent was killed before returning")
    if _trailing_errors(session.tools) >= THRESHOLDS["error_cascade"]:
        return ("error-cascade", "session ends on consecutive failing tool calls")
    if session.messages and session.messages[-1].role == "assistant":
        if any(e.subtype == "compact_boundary" for e in session.sysev[-5:]):
            return ("compacted-idle", "went idle shortly after a context compaction")
        return ("complete", "")
    if session.messages and session.messages[-1].role == "user":
        return ("interrupted-user", "last turn is a user message with no reply")
    return ("unknown", "no messages parsed")


#: Precedence order — earlier entries win. Same shape as the error taxonomy's ordered rules:
#: a single signal per session, chosen deterministically, so a downstream skill can key on it.
TAIL_SIGNALS: tuple[str, ...] = (
    "mid_tool", "error_tail", "apology", "completion_marker",
    "next_step_stated", "silent", "neutral",
)

_APOLOGY_RE = re.compile(r"\b(sorry|apologi[sz]e|unable to|can'?t|cannot)\b", re.I)
_COMPLETION_RE = re.compile(
    r"\b(done|complete[ds]?|finished|shipped|ready|works|all set|"
    r"passing|passes)\b", re.I)
_NEXT_STEP_RE = re.compile(
    r"(?:^|[\.\n])\s*(?:I(?:'|’)?ll|I will|next[,:]|next step|going to|"
    r"about to|let me)\b", re.I)


def derive_tail_signal(session: ParsedSession, tail_texts: dict[int, str]) -> str:
    """Classify the tail of a session for the ``unfinished-work`` skill.

    Args:
        session: Post-parse session, with ``err_class`` already annotated on tools.
        tail_texts: Map from ``message.line`` to that message's **full** text — the
            classifier needs uncapped strings because completion markers ("this is now
            done") frequently land past the 200-char preview cap. The caller decides how
            many trailing messages to include; only the entries whose line matches a
            message in ``session.messages`` are consulted.

    Returns:
        One value from ``TAIL_SIGNALS``. Never raises. Empty sessions return ``"neutral"``.
    """
    # mid_tool: any tool_use without a paired tool_result. `annotate_errors` already tags
    # these with err_class == "no-result" (parse.py leaves the flag set to False because
    # the tool did not produce a failing result — it produced no result at all).
    if any(t.err_class == "no-result" for t in session.tools):
        return "mid_tool"

    # error_tail: the final tool call actually failed with a classified taxonomy error.
    if session.tools:
        last = session.tools[-1]
        if last.is_error and last.err_class and last.err_class != "no-result":
            return "error_tail"

    if not session.messages:
        return "neutral"

    last_msg = session.messages[-1]

    # silent: the final turn is a user prompt with no assistant reply — the same signal
    # `derive_end_state` uses for `interrupted-user`, exposed here for the tail classifier.
    if last_msg.role == "user":
        return "silent"

    if last_msg.role != "assistant":
        return "neutral"

    text = tail_texts.get(last_msg.line, last_msg.preview or "")
    if _APOLOGY_RE.search(text):
        return "apology"
    if _COMPLETION_RE.search(text):
        return "completion_marker"
    if _NEXT_STEP_RE.search(text):
        return "next_step_stated"
    return "neutral"


def _trailing_errors(tools: list[ToolCall]) -> int:
    """Count failing tool calls at the tail of the session."""
    count = 0
    for call in reversed(tools):
        if call.is_error:
            count += 1
        else:
            break
    return count


def _repeat_tool(session: ParsedSession) -> list[Anomaly]:
    """Identical (tool, input) invoked repeatedly — the loop signal."""
    seen: dict[tuple[str, str], list[int]] = {}
    for call in session.tools:
        seen.setdefault((call.name, call.input_digest), []).append(call.line)
    out = []
    for (name, _dig), lines in seen.items():
        if len(lines) >= THRESHOLDS["repeat_tool"]:
            out.append(Anomaly("repeat-tool", f"{name} called with identical input",
                               len(lines), lines[:8]))
    return out


def _path_churn(session: ParsedSession, op: str, key: str, kind: str) -> list[Anomaly]:
    """Shared implementation for edit-thrash and read-loop detection."""
    counts: dict[str, int] = {}
    for f in session.files:
        if f.op == op:
            counts[f.path] = counts.get(f.path, 0) + 1
    limit = THRESHOLDS[key]
    return [Anomaly(kind, path, n, []) for path, n in counts.items() if n >= limit]


def _error_cascade(session: ParsedSession) -> list[Anomaly]:
    """Runs of consecutive failing tool calls."""
    out, run, lines = [], 0, []
    for call in session.tools:
        if call.is_error:
            run += 1
            lines.append(call.line)
        else:
            if run >= THRESHOLDS["error_cascade"]:
                out.append(Anomaly("error-cascade", "consecutive failures", run, lines[:8]))
            run, lines = 0, []
    if run >= THRESHOLDS["error_cascade"]:
        out.append(Anomaly("error-cascade", "consecutive failures", run, lines[:8]))
    return out


def _hook_pingpong(session: ParsedSession) -> list[Anomaly]:
    """Repeated hook blocks against the same tool — a self-inflicted retry loop."""
    counts: dict[str, list[int]] = {}
    for call in session.tools:
        if call.err_class == "hook-block":
            counts.setdefault(call.name, []).append(call.line)
    return [Anomaly("hook-pingpong", f"{name} blocked by a hook repeatedly", len(lines),
                    lines[:8])
            for name, lines in counts.items() if len(lines) >= THRESHOLDS["hook_pingpong"]]


def _agent_trouble(session: ParsedSession) -> list[Anomaly]:
    """Killed or never-returning subagents."""
    out = []
    kills = [e.line for e in session.sysev if e.subtype == "agents_killed"]
    if kills:
        out.append(Anomaly("agent-kill", "subagent killed", len(kills), kills[:8]))
    orphans = [c.line for c in session.tools
               if c.name == "Agent" and c.err_class == "no-result"]
    if orphans:
        out.append(Anomaly("orphan-subagent", "Agent spawned, never returned",
                           len(orphans), orphans[:8]))
    return out


def _compaction_churn(session: ParsedSession) -> list[Anomaly]:
    """Repeated context compaction — the session outgrew its window more than once."""
    lines = [e.line for e in session.sysev if e.subtype == "compact_boundary"]
    if len(lines) >= THRESHOLDS["compaction_churn"]:
        return [Anomaly("compaction-churn", "context compacted repeatedly", len(lines),
                        lines[:8])]
    return []


def _stalls(session: ParsedSession) -> list[Anomaly]:
    """Individual tool calls that ran far longer than the threshold."""
    limit = THRESHOLDS["stall_ms"]
    slow = [c for c in session.tools if c.dur_ms is not None and c.dur_ms > limit]
    return [Anomaly("stall", f"{c.name} took {c.dur_ms // 1000}s", 1, [c.line]) for c in slow]


def _rejection_persist(session: ParsedSession) -> list[Anomaly]:
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


_DETECTORS: list[Callable[[ParsedSession], list[Anomaly]]] = [
    _repeat_tool,
    lambda s: _path_churn(s, "edit", "file_thrash", "file-thrash"),
    lambda s: _path_churn(s, "read", "read_loop", "read-loop"),
    _error_cascade,
    _hook_pingpong,
    _agent_trouble,
    _compaction_churn,
    _stalls,
    _rejection_persist,
]


def detect(session: ParsedSession) -> list[Anomaly]:
    """Run every detector against a session.

    Args:
        session: A session whose errors have already been annotated.

    Returns:
        Anomalies sorted by count, most frequent first.
    """
    found: list[Anomaly] = []
    for detector in _DETECTORS:
        found.extend(detector(session))
    return sorted(found, key=lambda a: -a.count)
