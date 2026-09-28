"""The corpus, parsed on demand and held in memory.

There is no cache and no database. Every command parses the transcripts it needs, answers
from plain Python data structures, and exits. The rationale is measured and recorded in
``SPEC.md``: a session parses cold in ~172 ms against ~180 ms to read it back from the SQLite
cache that used to live here, so the cache bought nothing and cost an ingest step, a schema and
a staleness contract.

Session JSONL is append-only, which is what makes this safe: nothing rewrites history, so
there is no invalidation problem and no consistency window to protect.

This module owns the *post-parse pipeline*. ``parse_file`` alone returns a session with
``end_state="unknown"``, no error classes and no anomalies; the four steps in :func:`load_one`
are what turn it into something the reports can use.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sessionkit import live, parse_pi
from sessionkit import sources as src
from sessionkit.classify import annotate_errors, derive_end_state
from sessionkit.detectors import Anomaly, detect
from sessionkit.detectors.interference import resource_interference
from sessionkit.parse import ParsedSession, parse_file, project_key


@dataclass
class Loaded:
    """One transcript, parsed and fully annotated.

    ``project_key`` and ``anomalies`` are derived rather than parsed — they are not fields of
    :class:`~sessionkit.parse.ParsedSession` and only exist once :func:`load_one` has run.
    """

    session: ParsedSession
    project_key: str
    anomalies: list[Anomaly] = field(default_factory=list)

    @property
    def sid(self) -> str:
        return self.session.sid


@dataclass
class Corpus:
    """Everything reachable, parsed. Discarded when the process exits."""

    sources: list[src.Source] = field(default_factory=list)
    sessions: list[Loaded] = field(default_factory=list)
    failed: int = 0

    @property
    def unreachable(self) -> list[str]:
        """Source ids that could not be read, with the reason."""
        return [f"{s.id} ({s.note})" for s in self.sources if not s.reachable]


def transcripts(source: src.Source) -> list[tuple[Path, str]]:
    """Every session transcript under a source, paired with its project directory name.

    Claude Code / opencode use `projects/<project>/<session>.jsonl`, with subagent transcripts
    one level deeper at `projects/<project>/<parent-session>/subagents/agent-*.jsonl`. pi has
    no projects/ indirection: sessions/<project>/<file>.jsonl directly, plus pi-subagents
    async runs at sessions/<project>/<parent-stem>/<folder>/run-<n>/session.jsonl.
    """
    if source.kind == "pi":
        return _pi_transcripts(source)

    found: list[tuple[Path, str]] = []
    root = source.projects_dir
    if not root.is_dir():
        return found
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for project_dir in entries:
        try:
            files = sorted(project_dir.glob("*.jsonl"))
            files.extend(sorted(project_dir.glob("*/subagents/*.jsonl")))
        except OSError:
            continue
        found.extend((f, project_dir.name) for f in files)
    return found


def _pi_transcripts(source: src.Source) -> list[tuple[Path, str]]:
    """Every ``.jsonl`` session under a pi source, paired with its project directory name."""
    found: list[tuple[Path, str]] = []
    root = source.sessions_root
    if not root.is_dir():
        return found
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for project_dir in entries:
        try:
            files = sorted(project_dir.glob("*.jsonl"))
            files.extend(sorted(project_dir.glob("*/*/run-*/session.jsonl")))
        except OSError:
            continue
        found.extend((f, project_dir.name) for f in files)
    return found


def load_one(source: src.Source, path: Path, dir_name: str,
             live_sids: frozenset[str] = frozenset()) -> list[Loaded]:
    """Parse one transcript and derive everything the reports need from it.

    Returns one `Loaded` for a Claude Code/opencode transcript, or one `Loaded` for a pi
    session plus one more per synthetic subagent child its `subagent` tool calls produced
    (parse_pi.py, spec Sec 4) — always at least one entry.
    """
    if source.kind == "pi":
        return _load_pi(source, path, dir_name, live_sids)

    session = parse_file(path, source.id)
    if source.location == "container":
        session.cwd = src.rewrite_cwd(session.cwd, source.origin)
    annotate_errors(session)
    session.end_state, session.end_reason = derive_end_state(session)
    anomalies = detect(session, session.sid in live_sids)
    # tail_signal is left unset here: query.tail_signal() memoises onto the session on first
    # real use, and only `sk tail`/`sk files` ever ask for it — every other command paid for
    # a re-read of each session's last 6 lines from disk for nothing.
    return [Loaded(session, project_key(session.cwd, source.id, dir_name), anomalies)]


def _load_pi(source: src.Source, path: Path, dir_name: str,
             live_sids: frozenset[str]) -> list[Loaded]:
    """A pi session plus its synthetic subagent children, or one pi-subagents async run."""
    parent = parse_pi.parse_file(path, source.id)
    meta = _mark_async_child(parent, path) if _is_async_run(path) else {}
    annotate_errors(parent)
    parent.end_state, parent.end_reason = derive_end_state(parent)
    _apply_exit_code(parent, meta)
    loaded = [Loaded(parent, project_key(parent.cwd, source.id, dir_name),
                     detect(parent, parent.sid in live_sids))]
    for child in parent.child_sessions:
        annotate_errors(child)
        # end_state/end_reason were already assigned in parse_pi from the subagent result's
        # exitCode/stopReason — derive_end_state assumes a live tail of tool calls that a
        # synthetic child, built from an already-finished subprocess, does not have.
        loaded.append(Loaded(child, project_key(child.cwd, source.id, dir_name),
                             detect(child, child.sid in live_sids)))
    return loaded


def _is_async_run(path: Path) -> bool:
    """A pi-subagents async child run: ``<parent-stem>/<folder>/run-<n>/session.jsonl``."""
    return path.name == "session.jsonl" and path.parent.name.startswith("run-")


def _mark_async_child(session: ParsedSession, path: Path) -> dict[str, Any]:
    """Mark an async run as a subagent of the session its grandparent folder is named after.

    Returns the run's ``meta.json`` (empty when missing or unreadable) for the exit code.
    """
    parent_stem = path.parents[2].name
    session.is_subagent = True
    session.parent_sid = parent_stem.split("_", 1)[1] if "_" in parent_stem else parent_stem
    agent, run_id = parse_pi.async_run_identity(session)
    meta = _async_meta(path.parents[3] / "subagent-artifacts", run_id)
    session.agent_type = agent or str(meta.get("agent") or "")
    return meta


def _async_meta(folder: Path, run_id: str) -> dict[str, Any]:
    """The first readable ``<runId>_*_meta.json`` in ``folder``, or ``{}``."""
    if not run_id:
        return {}
    for candidate in sorted(folder.glob(f"{run_id}_*_meta.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _apply_exit_code(session: ParsedSession, meta: dict[str, Any]) -> None:
    """A run that exited non-zero did not complete, whatever its transcript's tail says."""
    code = meta.get("exitCode")
    if isinstance(code, bool) or not isinstance(code, int) or code == 0:
        return
    if session.end_state == "complete":
        session.end_state = "error-cascade"
        session.end_reason = f"subagent run exited with code {code}"


def _link_async_runs(sessions: list[Loaded]) -> None:
    """Resolve a parent's ``run:<runId>`` dispatch edges to the async child's session id."""
    runs: dict[str, str] = {}
    for entry in sessions:
        if entry.session.is_subagent:
            _agent, run_id = parse_pi.async_run_identity(entry.session)
            if run_id:
                runs[run_id] = entry.session.sid
    for entry in sessions:
        edges = entry.session.dispatch_edges
        if any(child.startswith("run:") for _call, child in edges):
            entry.session.dispatch_edges = [
                (call, runs.get(child[4:], child) if child.startswith("run:") else child)
                for call, child in edges]


def _add_cross_session_findings(sessions: list[Loaded]) -> None:
    """Findings that need several sessions at once (spec §2.2 ``resource-interference``)."""
    pairs = [(entry.project_key, entry.session) for entry in sessions]
    for entry, extra in zip(sessions, resource_interference(pairs)):
        entry.anomalies.extend(extra)


def _disambiguate(sessions: list[Loaded]) -> None:
    """Suffix a session id already claimed by a *different* transcript.

    Session ids are unique per file in practice, but nothing guarantees it — a resumed or
    copied transcript can repeat one, and two files in this corpus do. Without this the two
    would be indistinguishable in ``sk index`` and ``sk show`` would silently pick one. The
    suffix is derived from the path, so it is stable across runs and visible in reports
    rather than quietly merging two sessions.

    The first file to claim an id keeps it bare; later claimants are suffixed.
    """
    claimed: dict[str, str] = {}
    for entry in sessions:
        sid = entry.session.sid
        owner = claimed.get(sid)
        if owner is None:
            claimed[sid] = entry.session.path
        elif owner != entry.session.path:
            tag = hashlib.sha1(entry.session.path.encode("utf-8", "replace")).hexdigest()[:6]
            entry.session.sid = f"{sid}#{tag}"


def load_session(prefix: str) -> Loaded | None:
    """Parse only the transcripts whose *filename* could hold this session id.

    pi's filenames (`<timestamp>_<uuid>.jsonl`) don't share a prefix with either the header's
    session id or a synthetic child's `<sid>:<call_id>:<i>` id, so the filename pre-filter that
    speeds this up for Claude Code is skipped for pi sources — every pi file is parsed and
    checked. Acceptable for now (spec Sec 9); revisit if `sk show` proves slow on a large pi
    corpus.
    """
    needle = prefix.lower()
    found: list[Loaded] = []
    sources = src.scannable(src.discover())
    live_set = frozenset(live.live_sids({s.root for s in sources}))
    for source in sources:
        for path, dir_name in transcripts(source):
            if source.kind != "pi":
                stem = path.stem
                candidate = stem[6:] if stem.startswith("agent-") else stem
                if not candidate.lower().startswith(needle):
                    continue
            try:
                entries = load_one(source, path, dir_name, live_set)
            except OSError:
                continue
            for entry in entries:
                if entry.session.sid.lower().startswith(needle):
                    found.append(entry)
    if not found:
        return None
    return max(found, key=lambda e: e.session.ended_at or "")


def load(only: str | None = None) -> Corpus:
    """Discover every reachable source and parse its transcripts.

    Args:
        only: Restrict to a single source id.

    Returns:
        The parsed corpus, including the sources that could not be read so a report can say
        what it did not cover.
    """
    found = src.discover()
    corpus = Corpus(sources=found)
    scannable = src.scannable(found)
    live_set = frozenset(live.live_sids({s.root for s in scannable}))
    for source in scannable:
        if only and source.id != only:
            continue
        for path, dir_name in transcripts(source):
            try:
                corpus.sessions.extend(load_one(source, path, dir_name, live_set))
            except OSError:
                corpus.failed += 1
    _disambiguate(corpus.sessions)
    _link_async_runs(corpus.sessions)
    _add_cross_session_findings(corpus.sessions)
    return corpus
