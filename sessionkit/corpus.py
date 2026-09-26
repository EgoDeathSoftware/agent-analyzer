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
from dataclasses import dataclass, field
from pathlib import Path

from sessionkit import parse_pi
from sessionkit import sources as src
from sessionkit.classify import Anomaly, annotate_errors, derive_end_state, detect
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
    no `projects/` indirection at all: `sessions/<project>/<file>.jsonl` directly, and no
    separate subagent glob — nested subagent conversations are inline (see parse_pi.py).
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
        except OSError:
            continue
        found.extend((f, project_dir.name) for f in files)
    return found


def load_one(source: src.Source, path: Path, dir_name: str) -> list[Loaded]:
    """Parse one transcript and derive everything the reports need from it.

    Returns one `Loaded` for a Claude Code/opencode transcript, or one `Loaded` for a pi
    session plus one more per synthetic subagent child its `subagent` tool calls produced
    (parse_pi.py, spec Sec 4) — always at least one entry.
    """
    if source.kind == "pi":
        parent = parse_pi.parse_file(path, source.id)
        annotate_errors(parent)
        parent.end_state, parent.end_reason = derive_end_state(parent)
        loaded = [Loaded(parent, project_key(parent.cwd, source.id, dir_name),
                         detect(parent))]
        for child in parent.child_sessions:
            annotate_errors(child)
            # end_state/end_reason were already assigned in parse_pi from the subagent
            # result's exitCode/stopReason (spec Sec 4) — derive_end_state assumes a live
            # tail of tool calls a synthetic child, built from an already-finished
            # subprocess, does not have.
            loaded.append(Loaded(child, project_key(child.cwd, source.id, dir_name),
                                 detect(child)))
        return loaded

    session = parse_file(path, source.id)
    if source.location == "container":
        session.cwd = src.rewrite_cwd(session.cwd, source.origin)
    annotate_errors(session)
    session.end_state, session.end_reason = derive_end_state(session)
    anomalies = detect(session)
    # tail_signal is left unset here: query.tail_signal() memoises onto the session on first
    # real use, and only `sk tail`/`sk files` ever ask for it — every other command paid for
    # a re-read of each session's last 6 lines from disk for nothing.
    return [Loaded(session, project_key(session.cwd, source.id, dir_name), anomalies)]


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
    for source in src.scannable(src.discover()):
        for path, dir_name in transcripts(source):
            if source.kind != "pi":
                stem = path.stem
                candidate = stem[6:] if stem.startswith("agent-") else stem
                if not candidate.lower().startswith(needle):
                    continue
            try:
                entries = load_one(source, path, dir_name)
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
    for source in src.scannable(found):
        if only and source.id != only:
            continue
        for path, dir_name in transcripts(source):
            try:
                corpus.sessions.extend(load_one(source, path, dir_name))
            except OSError:
                corpus.failed += 1
    _disambiguate(corpus.sessions)
    return corpus
