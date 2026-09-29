"""The failure taxonomy: every error class and finding kind, in one table.

Each entry names a failure, says how it is detected (its tier), where its fix belongs (its fix
target), and gives a one-line hint. Error classes also carry the text patterns that recognise
them in a tool result; their order matters because patterns overlap — a hook block also
contains "exit".

The error-class list is grounded in the live corpus rather than guessed: clustering the 208
error results across 86 transcripts put ``hook-block`` first at 47 occurrences (23% of all
failures). Text that matches nothing lands in ``other`` with its normalised prefix retained,
so the taxonomy grows from real misses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TIER_S = "S"
TIER_CANDIDATE = "S->J"
TIER_J = "J"

#: All tuneable detector limits in one place rather than scattered as literals.
THRESHOLDS: dict[str, float] = {
    "repeat_tool": 3,       # identical (tool, input) calls in one session
    "file_thrash": 12,     # edits to the same path
    "file_thrash_min": 5,   # ... or this many edits to one path
    "file_thrash_failures": 2,  # ... when at least this many of them failed
    "read_loop": 5,         # reads of the same path
    "error_cascade": 3,     # consecutive failing tool calls
    "edit_mismatch_spiral": 3,  # edit-mismatch failures on one path
    "read_only_spiral": 12,  # consecutive read/search calls
    "read_only_text_chars": 200,  # an assistant answer at least this long breaks the run
    "hook_pingpong": 2,     # hook blocks against the same tool
    "compaction_churn": 2,  # compaction events
    "stall_ms": 120_000,    # a single tool call taking longer than this
    "stall_exec_ms": 600_000,        # a command with no declared timeout running this long
    "stall_timeout_fraction": 0.9,   # a command reaching this share of its declared timeout
    "degeneration_ratio": 0.5,          # repeated word 8-grams within one message
    "generation_stall_ms": 600_000,     # a model turn slower than this ...
    "generation_stall_factor": 5,       # ... and this many times the session's median
    "drift_min_calls": 8,         # an unreferenced run needs at least this many calls ...
    "drift_min_share": 0.4,       # ... and this share of its turn's calls
    "interrupt_drift_calls": 8,   # calls cited before a user interrupt
    "interference_exec_ms": 120_000,  # a concurrent command at least this long can interfere
}


@dataclass(frozen=True)
class FailureClass:
    """One named failure: an error class matched in result text, or a finding kind.

    ``fix_target`` is where the remedy belongs: ``model``, ``harness`` (instructions file, tool
    config, token limits), ``serving`` (vLLM parser, chat template, sampling), ``env``, or
    ``none``.
    """

    name: str
    family: str
    tier: str
    fix_target: str
    hint: str
    patterns: tuple[re.Pattern[str], ...] = ()


def _error(name: str, family: str, fix_target: str, hint: str, *regexes: str,
           flags: int = re.I) -> FailureClass:
    """An error class: structural tier, recognised by its patterns."""
    patterns = tuple(re.compile(r, flags) for r in regexes)
    return FailureClass(name, family, TIER_S, fix_target, hint, patterns)


def _kind(name: str, family: str, tier: str, fix_target: str, hint: str) -> FailureClass:
    """A finding kind: emitted by a detector, or (tier J) judged by the skill."""
    return FailureClass(name, family, tier, fix_target, hint)


#: Ordered: the first class whose pattern matches wins, so specific classes precede generic
#: ones (a denied mkdir arrives as "Exit code 1 ... Permission denied"; a pi bash failure ends
#: with "Command exited with code N" after its real cause).
ERROR_CLASSES: tuple[FailureClass, ...] = (
    _error("hook-block", "environment", "harness",
           "Promote the rule into CLAUDE.md (AGENTS.md for pi) so the command is never "
           "emitted.",
           r"PreToolUse:|PostToolUse:.*hook error|\bBLOCKED:"),
    _error("user-rejected", "environment", "none",
           "Approach problem — the model proposed something the user declined.",
           r"user doesn't want to (?:proceed|take this action)|tool use was rejected"),
    # Distinct from user-rejected: no human saw this one, the harness blocked it.
    _error("policy-denied", "environment", "harness",
           "Blocked by permission policy — needs approval or a different approach.",
           r"denied by the Claude Code|auto mode classifier|"
           r"permission for this action was denied|"
           r"permission to use \w+.{0,200}?has been denied|"
           r"File is in a directory that is denied by your permission|"
           r"This session is isolated in the worktree|Tool execution was blocked",
           flags=re.I | re.S),
    _error("tool-error-structured", "environment", "env",
           'A tool returned a structured failure ({"ok": false}) — read its errors list; the '
           "input usually broke the tool's own rules.",
           r'^\s*\{\s*"ok"\s*:\s*false'),
    # Before invalid-args: pi silently repairs unparseable tool-call JSON to {}, so a
    # validation error on empty arguments means the serving stack lost them.
    _error("args-dropped", "tool-protocol", "serving",
           "The tool call reached the harness with empty arguments — the serving stack's "
           "tool-call parser lost them. Check the vLLM tool parser and chat template.",
           r"Received arguments:\s*\{\s*\}"),
    _error("invalid-args", "tool-protocol", "model",
           "Malformed tool call — the model passed arguments that fail the tool's schema.",
           r"InputValidationError|parameter `\w+` type is expected|"
           r'Validation failed for tool "|Invalid arguments|is required when'),
    _error("unknown-tool", "tool-protocol", "model",
           "The model called a tool that does not exist — often a sibling tool's name or one "
           "from another harness. Check the tool list it was given.",
           r"\bTool [\w.-]+ not found|No such tool available|Unknown skill:"),
    _error("truncated-call", "tool-protocol", "harness",
           "A tool call was cut off by the output token limit and never ran. Raise the model's "
           "maxTokens, or have it write in smaller pieces.",
           r"was not executed: the response hit the output token limit"),
    _error("edit-mismatch", "tool-protocol", "model",
           "The edit's old text did not match the file (whitespace, a stale view, or not "
           "unique). Re-read the exact region before editing.",
           r"String to replace not found|Found \d+ matches of the string|"
           r"Could not find the exact text in|Could not find edits\[|"
           r"Found \d+ occurrences of the text|No changes made to|"
           r"edits\[\d+\] and edits\[\d+\] overlap"),
    _error("tool-misuse", "tool-protocol", "model",
           "A tool was used on the wrong kind of target (a directory for a file read, an "
           "offset past the end). Check the path or range first.",
           r"\bEISDIR\b|Offset \d+ is beyond end of file"),
    _error("sibling-cascade", "tool-protocol", "none",
           "A parallel call failed only because a sibling in the same batch failed — look at "
           "the sibling's error instead.",
           r"Sibling tool call errored"),
    _error("aborted", "lifecycle", "none",
           "The user stopped this call or turn.",
           # pi's abort status is the body's last line; mid-body it is a stack-trace detail.
           r"\[Request interrupted by user", r"\b(?:Operation|Command) aborted\s*$"),
    _error("file-too-large", "tool-protocol", "model",
           "Read with offset/limit, or use Grep instead of a full read.",
           r"exceeds maximum allowed tokens"),
    _error("read-truncated", "tool-protocol", "model",
           "Narrow the read; the model only saw part of the file.",
           r"read_truncation|truncated to \d+ lines"),
    _error("stale-read", "tool-protocol", "model",
           "Re-read the file before editing; another writer changed it.",
           r"has been modified since read|file has changed since"),
    _error("unread-file", "tool-protocol", "model",
           "Read before Write/Edit — the tool requires it.",
           r"has not been read yet|read it first"),
    _error("missing-tool", "environment", "env",
           "Install the tool or drop the dependency on it.",
           r"command not found|: not found$|\bno such command\b"),
    # Must precede exit-code: a denied mkdir surfaces as "Exit code 1 ... Permission denied".
    _error("permission-denied", "environment", "env",
           "Write target is not writable by this user — pick a writable path.",
           r"EACCES|permission denied|EPERM\b"),
    _error("not-a-repo", "generic", "none",
           "cwd assumption — the command ran outside a repo.",
           r"not a git repository"),
    _error("not-found", "generic", "none",
           "Path assumption — verify the path before operating on it.",
           r"File does not exist|Directory does not exist|no such file or directory|"
           r"\bENOENT\b"),
    _error("rate-limit", "environment", "env",
           "Back off, or route to an alternate provider.",
           r"rate limit|429\b|too many requests"),
    _error("api-error", "environment", "env",
           "Transient or misconfigured endpoint — check which.",
           r"request failed with status code|\b5\d\d\b error"),
    _error("mcp-error", "environment", "env",
           "MCP server health — check the server, not the prompt.",
           r"error in mcp__|mcp server .* (failed|error)|No active transport"),
    _error("timeout", "generic", "none",
           "Raise the timeout or split the work.",
           r"timed out|timeout exceeded"),
    _error("exit-code", "generic", "none",
           "Genuine command failure — read the command output.",
           r"^exit code \d+", r"Command exited with code \d+\s*$"),
)

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
#: pi appends the status to the *end* of bash output: "...\n\nCommand exited with code N".
_PI_EXIT_RE = re.compile(r"Command exited with code (\d+)\s*$", re.I)

#: Reached only through ``_EXIT_CODES``, so it has no text patterns of its own.
KILLED = _kind("killed", "generic", TIER_S, "env",
               "Killed (SIGKILL, usually OOM) or terminated — the work did not complete.")

_OVERFLOW = _error(
    "context-overflow", "environment", "harness",
    "The prompt plus the reserved output tokens exceeded the context window. Lower the "
    "model's maxTokens reservation (32K reserved on a 131K window leaves 98K for the "
    "conversation) or compact earlier; splitting the work is the fallback.",
    r"maximum context length|context length exceeded|context window|prompt is too long|"
    r"Context overflow recovery failed")
_AUTH = _error(
    "auth", "environment", "env",
    "The provider sign-in is missing or expired — re-authenticate. Not a model or prompt "
    "problem.",
    r"authentication_failed|invalid_grant|oauth|\b401\b|\b403\b|please run /login|"
    r"not logged in|login expired|unauthori[sz]ed")
_NETWORK = _error(
    "network", "environment", "env",
    "The model endpoint was unreachable or too slow to answer — check the server's health "
    "and load, and whether something else was using it at the time.",
    r"ECONNREFUSED|ECONNRESET|ENOTFOUND|getaddrinfo|fetch failed|socket hang up|timed out|"
    r"timeout|network error")
API_ERROR_CLASSES: tuple[FailureClass, ...] = (_OVERFLOW, _AUTH, _NETWORK)

#: Ordered API-error rules. ``rate-limit`` reuses the tool-result class name with patterns
#: that match provider wording ("rate_limit", "hit your session limit").
_API_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (_OVERFLOW.name, _OVERFLOW.patterns),
    (_AUTH.name, _AUTH.patterns),
    ("rate-limit", (re.compile(
        r"rate.?limit|hit your (?:session )?limit|\b429\b|too many requests|quota", re.I),)),
    (_NETWORK.name, _NETWORK.patterns),
)

FINDING_KINDS: tuple[FailureClass, ...] = (
    _kind("repeat-tool", "loop", TIER_S, "model",
          "Same call retried with identical input — check why the prior result wasn't used "
          "instead of re-running it."),
    _kind("file-thrash", "loop", TIER_S, "model",
          "Same file edited repeatedly — plan the full change before editing, or use "
          "MultiEdit for a single pass."),
    _kind("read-loop", "loop", TIER_S, "model",
          "Same file re-read repeatedly — remember its contents instead of re-reading."),
    _kind("error-cascade", "loop", TIER_S, "model",
          "Consecutive failures — stop and diagnose the root cause instead of retrying "
          "variants of the same call."),
    _kind("hook-pingpong", "loop", TIER_S, "harness",
          "The same tool was blocked by a hook repeatedly — the rule belongs in CLAUDE.md, "
          "where it teaches at generation time instead of blocking at execution time."),
    _kind("agent-kill", "lifecycle", TIER_S, "none",
          "A subagent was killed before returning — check the dispatch conditions or the "
          "timeout that ended it."),
    _kind("orphan-subagent", "lifecycle", TIER_S, "harness",
          "A subagent was spawned and never returned — check whether it stalled or the "
          "parent moved on without waiting."),
    _kind("compaction-churn", "lifecycle", TIER_S, "harness",
          "Context was compacted more than once — the session outgrew its window; consider "
          "splitting the work or narrowing scope."),
    _kind("stall", "lifecycle", TIER_S, "env",
          "A single tool call ran unusually long — check for a network hang or an unbounded "
          "operation."),
    _kind("rejection-persist", "loop", TIER_S, "model",
          "A call was retried after the user explicitly rejected it — respect the rejection "
          "and ask before retrying, rather than trying again unprompted."),
    _kind("edit-mismatch-spiral", "loop", TIER_S, "model",
          "Edits to the same file failed repeatedly — the model is editing from a stale or "
          "imagined view of the file. Re-read the exact region before each edit."),
    _kind("read-only-spiral", "loop", TIER_S, "model",
          "A long run of reads and searches with no edit, command or answer — the model is "
          "gathering instead of acting. Ask for a plan or a first change."),
    _kind("tool-text-leak", "tool-protocol", TIER_S, "serving",
          "The model wrote a tool call as plain text, so nothing ran and the turn ended. "
          "Check the serving stack's tool-call parser and chat template for this model "
          "(vLLM --tool-call-parser); long contexts make it worse."),
    _kind("empty-turn", "generation", TIER_S, "serving",
          "The model ended its turn with no text and no tool call — usually reasoning-only "
          "output. Check the chat template and reasoning parser, or lower the thinking level."),
    _kind("degeneration", "generation", TIER_S, "serving",
          "The model repeated itself within one turn — a sampling degeneration loop. Raise "
          "temperature or add a repetition penalty for this model, lower the thinking level, "
          "or keep the context smaller before hard steps."),
    _kind("output-truncated", "generation", TIER_S, "harness",
          "The response hit the output token limit mid-turn. Raise the model's maxTokens, or "
          "ask for smaller steps (write files in parts)."),
    _kind("generation-stall", "generation", TIER_S, "serving",
          "One model turn took far longer than the session's norm — a runaway reasoning loop "
          "or an overloaded server. Check that turn's reasoning for repetition and the "
          "server's load at the time."),
    _kind("hung", "lifecycle", TIER_S, "env",
          "The session ended waiting on a call or a model turn that never finished — check the "
          "tool or server that was pending."),
    _kind("abrupt-truncation", "lifecycle", TIER_S, "none",
          "The transcript ends in a partial write — the agent process died mid-record."),
    _kind("user-interrupt", "lifecycle", TIER_S, "none",
          "The user stopped the agent and redirected it — the work just before the interrupt "
          "is the likely wrong turn."),
    _kind("error-ignored", "recovery", TIER_CANDIDATE, "model",
          "A tool failed and the next message claims success — confirm whether the failure "
          "was actually handled."),
    _kind("give-up", "recovery", TIER_CANDIDATE, "model",
          "The session stopped after a single failure that may have been recoverable."),
    _kind("unverified-completion", "alignment", TIER_CANDIDATE, "harness",
          "The agent claimed completion without running tests or a build after its last "
          "change. Require verification in the instructions file."),
    _kind("premature-stop", "alignment", TIER_CANDIDATE, "model",
          "The session ends on a stated next step that never happened."),
    _kind("scope-creep", "alignment", TIER_CANDIDATE, "harness",
          "The ask was to review or explain, but files were changed. State read-only intent "
          "explicitly in the ask or the instructions file."),
    _kind("drift-candidate", "alignment", TIER_CANDIDATE, "harness",
          "A large share of a turn went to files or commands the ask never referenced, or the "
          "user interrupted it — confirm whether it was needed investigation or a detour."),
    _kind("false-premise", "alignment", TIER_J, "model",
          "The agent acted on an assumption it never checked. Walk back from the first "
          "failure to the first unverified assumption."),
    _kind("spec-neglect", "alignment", TIER_J, "harness",
          "A stated requirement of the ask was dropped. List the requirements and mark which "
          "were met."),
    _kind("misread-output", "alignment", TIER_J, "model",
          "A tool result contradicted the agent's next action — it misread or ignored what "
          "the tool said."),
    _kind("ignored-own-warning", "alignment", TIER_J, "model",
          "The agent stated a hazard or constraint, then acted against it."),
    _kind("role-drift", "alignment", TIER_J, "harness",
          "An orchestrator did its delegate's work in parallel with the delegate, duplicating "
          "effort and competing for shared resources."),
    _kind("resource-interference", "environment", TIER_CANDIDATE, "harness",
          "Model errors coincided with another session's long-running command in the same "
          "project — check whether that command used the resource the failing session depends "
          "on (for example, the model server)."),
)

ALL_CLASSES: tuple[FailureClass, ...] = (*ERROR_CLASSES, KILLED, *API_ERROR_CLASSES,
                                         *FINDING_KINDS)
_REGISTRY: dict[str, FailureClass] = {c.name: c for c in ALL_CLASSES}

_NORMALISE = [
    (re.compile(r"/[\w./\-]{6,}"), "<PATH>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def lookup(name: str) -> FailureClass | None:
    """The taxonomy entry for an error class or finding kind, or ``None``."""
    return _REGISTRY.get(name)


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
    code = _exit_code(body)
    if code is not None and code in _EXIT_CODES:
        return _EXIT_CODES[code]
    for entry in ERROR_CLASSES:
        if any(p.search(body) for p in entry.patterns):
            return (entry.name, entry.hint)
    return ("other", "")


def _exit_code(body: str) -> int | None:
    """The exit code in Claude Code's ``Exit code N`` prefix or pi's trailing suffix."""
    match = _EXIT_RE.match(body) or _PI_EXIT_RE.search(body)
    return int(match.group(1)) if match else None


def classify_api_error(text: str) -> str:
    """Classify a failed model call's error text (``Message.error_text``).

    Returns:
        ``context-overflow``, ``auth``, ``rate-limit``, ``network``, the catch-all
        ``api-error``, or ``""`` for empty text.
    """
    body = (text or "").strip()
    if not body:
        return ""
    for name, patterns in _API_RULES:
        if any(p.search(body) for p in patterns):
            return name
    return "api-error"


def signature(text: str, limit: int = 80) -> str:
    """Collapse an error body into a clusterable signature.

    Digits, hex and long paths are the parts that differ between otherwise-identical failures,
    so they are replaced with placeholders before truncation.
    """
    body = (text or "").strip()
    for pattern, replacement in _NORMALISE:
        body = pattern.sub(replacement, body)
    return body[:limit]
