"""Alignment candidates (tier S->J): did the work match what the user asked for?"""

from __future__ import annotations

import posixpath
import re
import sys
from dataclasses import dataclass

from sessionkit.classify import tail_text_signal
from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.detectors.lifecycle import user_interrupt
from sessionkit.features import claims_done
from sessionkit.harness import (call_paths, command_of, is_interrupt_marker, is_verify_command,
                                role, run_key)
from sessionkit.parse import Message, ParsedSession, ToolCall
from sessionkit.taxonomy import THRESHOLDS

_REVIEW = re.compile(
    r"\b(?:review|explain|analy[sz]e|audit|summari[sz]e|describe|what does|why does|"
    r"read-only|don'?t (?:change|edit|modify)|do not (?:change|edit|modify))\b", re.I)
#: Each verb also matches its inflected forms (fixes/fixed/fixing, ...) — a subagent dispatch
#: prompt phrased "you are fixing two issues found in a review" is an implement ask, not a
#: review-only one, even though it contains the word "review".
_IMPLEMENT = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|implement(?:s|ed|ing)?|add(?:s|ed|ing)?|"
    r"chang(?:e(?:s|d)?|ing)|refactor(?:s|ed|ing)?|updat(?:e(?:s|d)?|ing)|"
    r"writ(?:e|es|ing)|wrote|written|creat(?:e(?:s|d)?|ing)|"
    r"build(?:s|ing)?|built|mak(?:e|es|ing)|made|edit(?:s|ed|ing)?|"
    r"modif(?:y|ies|ied|ying)|remov(?:e(?:s|d)?|ing)|delet(?:e(?:s|d)?|ing)|"
    r"renam(?:e(?:s|d)?|ing))\b", re.I)
_NEGATED = re.compile(r"\b(?:don'?t|do not|never|without)\s+\w+", re.I)
_CHANGE_ROLES = ("edit", "write")


@dataclass
class TurnSpan:
    """One user ask and the tool calls made before the next ask."""

    ask: Message
    calls: list[ToolCall]


def _is_ask(m: Message) -> bool:
    """A real user ask: a typed prompt, not a harness-injected record or Claude Code's
    interrupt marker."""
    return m.is_prompt and not is_interrupt_marker(m.preview)


def user_turns(session: ParsedSession) -> list[TurnSpan]:
    """Split a session at each real user ask: a typed prompt, not a harness-injected record
    or Claude Code's interrupt marker."""
    asks = [m for m in session.messages if _is_ask(m)]
    spans = []
    for index, ask in enumerate(asks):
        end = asks[index + 1].line if index + 1 < len(asks) else sys.maxsize
        spans.append(TurnSpan(ask, [c for c in session.tools if ask.line < c.line < end]))
    return spans


def scope_creep(session: ParsedSession) -> list[Anomaly]:
    """The ask was to review or explain, and the turn changed files anyway."""
    out: list[Anomaly] = []
    for turn in user_turns(session):
        ask = turn.ask.preview
        if not _REVIEW.search(ask) or _IMPLEMENT.search(_NEGATED.sub("", ask)):
            continue
        changes = [c.line for c in turn.calls
                   if role(session.harness, c.name) in _CHANGE_ROLES and not c.is_error]
        if changes:
            out.append(Anomaly("scope-creep",
                               "the ask was to review or explain, but the turn changed files",
                               len(changes), [turn.ask.line] + changes[:MAX_LINES - 1]))
    return out


def unverified_completion(session: ParsedSession) -> list[Anomaly]:
    """A final completion claim with no test/build command after the last successful change."""
    if not session.messages or session.messages[-1].role != "assistant":
        return []
    final = session.messages[-1]
    if not claims_done(session.final_text or final.preview):
        return []
    changes = [i for i, c in enumerate(session.tools)
               if role(session.harness, c.name) in _CHANGE_ROLES and not c.is_error]
    if not changes:
        return []
    last_change = changes[-1]
    later = session.tools[last_change + 1:]
    if any(role(session.harness, c.name) == "exec" and is_verify_command(command_of(c))
           for c in later):
        return []
    return [Anomaly("unverified-completion",
                    "claims the work is done, but no test or build ran after the last change",
                    1, [session.tools[last_change].line, final.line])]


def premature_stop(session: ParsedSession) -> list[Anomaly]:
    """A session that ended normally on a stated next step."""
    if session.end_state != "complete" or not session.messages:
        return []
    final = session.messages[-1]
    if tail_text_signal(session.final_text or final.preview) != "next_step_stated":
        return []
    return [Anomaly("premature-stop", "the session ends on a stated next step", 1,
                    [final.line])]


@dataclass
class Run:
    """Consecutive calls in one turn that share a run key (a directory or an exec program)."""

    key: str
    lines: list[int]
    paths: list[str]
    referenced: bool = True


@dataclass
class TurnProfile:
    """Where one turn's effort went, relative to what its ask referenced."""

    ask_line: int
    ask_preview: str
    refs: list[str]
    calls: int
    runs: list[Run]


def focus_profile(session: ParsedSession) -> list[TurnProfile]:
    """Per user turn: the ask, its references, and the runs of calls it produced."""
    return [_profile(session, turn) for turn in user_turns(session)]


def _profile(session: ParsedSession, turn: TurnSpan) -> TurnProfile:
    runs: list[Run] = []
    for call in turn.calls:
        key = run_key(session.harness, call)
        paths = call_paths(session.harness, call)
        if runs and runs[-1].key == key:
            runs[-1].lines.append(call.line)
            runs[-1].paths.extend(paths)
        else:
            runs.append(Run(key, [call.line], list(paths)))
    _mark_referenced(runs, turn.ask.refs)
    return TurnProfile(turn.ask.line, turn.ask.preview, list(turn.ask.refs), len(turn.calls),
                       runs)


def _mark_referenced(runs: list[Run], refs: list[str]) -> None:
    """A run is referenced if it touches something the ask named, or a directory the turn
    already reached through something the ask named. A run with no paths stays referenced:
    there is no evidence either way."""
    if not refs:
        return
    reached: set[str] = set()
    for run in runs:
        if not run.paths:
            continue
        run.referenced = _program_named(run.key, refs) or any(
            _names(path, refs) or posixpath.dirname(path) in reached for path in run.paths)
        if run.referenced:
            reached.update(posixpath.dirname(p) for p in run.paths)


def _names(path: str, refs: list[str]) -> bool:
    base = posixpath.basename(path.rstrip("/"))
    for ref in refs:
        clean = ref.lstrip("@").rstrip("/")
        if "/" in clean and (clean in path or path in clean):
            return True
        if base and base == posixpath.basename(clean):
            return True
        if "/" not in clean and "." not in clean and len(clean) >= 3 and clean in path:
            return True
    return False


def _program_named(key: str, refs: list[str]) -> bool:
    return key.startswith("exec:") and key[len("exec:"):] in refs


def drift_candidates(session: ParsedSession) -> list[Anomaly]:
    """Unreferenced runs that dominate a turn, plus the calls before each user interrupt."""
    out: list[Anomaly] = []
    for profile in focus_profile(session):
        if not profile.refs:
            continue
        for run in profile.runs:
            share = len(run.lines) / profile.calls
            if (run.referenced or len(run.lines) < THRESHOLDS["drift_min_calls"]
                    or share < THRESHOLDS["drift_min_share"]):
                continue
            out.append(Anomaly("drift-candidate",
                               f"{len(run.lines)} calls ({share:.0%} of the turn) on {run.key}, "
                               "which the ask never referenced",
                               len(run.lines), run.lines[:MAX_LINES]))
    out.extend(_interrupt_drift(session))
    return out


def _interrupt_drift(session: ParsedSession) -> list[Anomaly]:
    """The user stopping the agent and redirecting it is direct evidence the preceding work
    went off course — cite the calls just before the interrupt, refs or not."""
    size = int(THRESHOLDS["interrupt_drift_calls"])
    out: list[Anomaly] = []
    for interrupt in user_interrupt(session):
        point = interrupt.lines[0]
        ask_line = max((m.line for m in session.messages if _is_ask(m) and m.line < point),
                       default=0)
        window = [c.line for c in session.tools if ask_line < c.line <= point][-size:]
        if window:
            out.append(Anomaly("drift-candidate",
                               f"the {len(window)} calls before the user interrupt at line "
                               f"{point}", len(window), window))
    return out
