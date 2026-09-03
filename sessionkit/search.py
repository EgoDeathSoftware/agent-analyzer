"""Full-corpus text search over raw transcript lines.

Kept separate from ``query.py`` because it runs *before* a session is parsed: matching happens
against the raw JSONL line so a spilled tool result's full text (retained in the transcript's
``toolUseResult`` field, not the ~2 KB ``message.content`` stub) is still found even though
every in-memory preview caps at 200/2000 chars (``PLAN.md`` §3.2.1). Only files that already
produced a hit get parsed by :func:`sessionkit.corpus.load_one`, which is what keeps a
full-corpus scan close to the few-millisecond ``rg`` baseline in ``SPEC.md`` §2 rather than
paying the ~1.2 s full-parse cost up front.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sessionkit import corpus, query
from sessionkit.corpus import Loaded
from sessionkit.redact import redact as redact_text
from sessionkit.sources import Source, scannable


def compile_query(text: str, *, regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    """Compile a search query into a pattern.

    Args:
        text: The literal text (or, with ``regex=True``, the pattern) to search for.
        regex: Treat ``text`` as a regular expression rather than a literal substring.
        case_sensitive: Match case exactly rather than folding case.

    Returns:
        A compiled pattern; ``pattern.search(line)`` finds a hit on one raw transcript line.

    Raises:
        SystemExit: ``text`` is empty, or ``regex=True`` and ``text`` does not compile — a
            silently-ignored bad pattern would report zero hits rather than a usage error.
    """
    if not text:
        raise SystemExit("search query must not be empty")
    flags = 0 if case_sensitive else re.IGNORECASE
    needle = text if regex else re.escape(text)
    try:
        return re.compile(needle, flags)
    except re.error as exc:
        raise SystemExit(f"bad --regex pattern {text!r}: {exc}") from None


#: A file-level safety valve, not a user-facing setting: a pathologically common query (a bare
#: word appearing hundreds of times in one huge transcript) should not force-read the whole
#: match set before :func:`search_rows` even gets to apply ``--per-session``/``--limit``.
_MAX_HITS_PER_FILE = 50


@dataclass
class Hit:
    """One raw-text match, not yet resolved to a session.

    ``line_no`` is the 1-based JSONL line for a ``kind="jsonl"`` hit, or ``0`` for a
    ``kind="spill"`` hit until :func:`search_rows` maps it back to the transcript line whose
    tool call produced the spilled result.
    """

    kind: str
    source_id: str
    dir_name: str
    path: str
    line_no: int
    raw_line: str


def scan_corpus(sources: list[Source], pattern: re.Pattern[str], *,
                max_hits_per_file: int = _MAX_HITS_PER_FILE) -> list[Hit]:
    """Raw substring/regex scan of every reachable source's transcripts, plus any spilled
    tool-result file.

    No parsing happens here — this is the fast pass PLAN.md §7 Phase 4 measures at a few
    milliseconds full-corpus (SPEC.md §2, `rg` over the whole corpus). Only files that
    match at all get parsed, by :func:`search_rows`.

    Args:
        sources: Candidate sources, as returned by ``sessionkit.sources.discover()``.
        pattern: Compiled query from :func:`compile_query`.
        max_hits_per_file: Stop recording further hits in one file past this count (a large
            file with a very common query still returns quickly); other files keep scanning.

    Returns:
        Every hit found, in ``sources``/``corpus.transcripts`` order, transcript hits before
        spill-file hits within each source.
    """
    hits: list[Hit] = []
    for source in scannable(sources):
        for path, dir_name in corpus.transcripts(source):
            hits.extend(_scan_jsonl(source, path, dir_name, pattern, max_hits_per_file))
        hits.extend(_scan_spill_files(source, pattern))
    return hits


def _scan_jsonl(source: Source, path: Path, dir_name: str, pattern: re.Pattern[str],
                max_hits: int) -> list[Hit]:
    """Match ``pattern`` against every raw line of one transcript.

    Matches the whole raw line, not any parsed field — a spilled tool result's full text lives
    in the transcript's ``toolUseResult`` field, on the same line as the truncated
    ``message.content`` stub, so a whole-line scan sees it even though nothing else in
    sessionkit keeps that field in memory (PLAN.md §3.2.1).
    """
    found: list[Hit] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if pattern.search(line):
                    found.append(Hit("jsonl", source.id, dir_name, str(path), line_no, line))
                    if max_hits and len(found) >= max_hits:
                        break
    except OSError:
        return found
    return found


def _scan_spill_files(source: Source, pattern: re.Pattern[str]) -> list[Hit]:
    """Match ``pattern`` against every ``<project>/<sid>/tool-results/*.txt`` file.

    This third copy of a spilled result (PLAN.md §3.2.1) is needed only when the
    transcript's own ``toolUseResult`` copy has aged out from under it — the common case is
    already covered by :func:`_scan_jsonl`, so this pass exists purely for that fallback.
    Unlike :func:`_scan_jsonl`, there is no hit cap here: one spill file can only ever
    produce one hit (the whole file is the unit), so a "hits per file" cap doesn't apply.
    """
    found: list[Hit] = []
    root = source.projects_dir
    if not root.is_dir():
        return found
    try:
        spill_files = sorted(root.glob("*/*/tool-results/*.txt"))
    except OSError:
        return found
    for spill_path in spill_files:
        try:
            text = spill_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            dir_name = spill_path.parent.parent.parent.name
            found.append(Hit("spill", source.id, dir_name, str(spill_path), 0, text))
    return found


Row = dict[str, Any]


def search_rows(sources: list[Source], scope: query.Filter, pattern: re.Pattern[str], *,
                context: int = 2, per_session: int = 1, limit: int = 20
                ) -> tuple[list[Row], int, int]:
    """Search rows for `sk search`: fast raw scan, then on-demand parse of only the files that
    matched, for scope filtering — and only *then* excerpt building, after capping, so a huge
    matching file never pays for more excerpts than will actually be shown.

    Args:
        sources: Candidate sources, as returned by ``sessionkit.sources.discover()``.
        scope: The shared ``--since``/``--project``/``--source``/``--subagents`` filter.
        pattern: Compiled query from :func:`compile_query`.
        context: Timeline rows of context to include before/after each hit's line.
        per_session: Max rows kept per session after sorting newest-first; ``0`` for unlimited.
        limit: Max rows returned overall; ``0`` for unlimited.

    Returns:
        ``(rows, degraded, total)``. ``degraded`` counts spill-file matches whose transcript
        could not be resolved (missing entirely, or no longer names the matched
        ``tool_use_id``), reported by the caller rather than silently dropped. ``total`` is
        the in-scope, deduplicated candidate count *before* ``per_session``/``limit`` capping,
        so a caller can report when rows were hidden rather than silently truncating.
    """
    hits = scan_corpus(sources, pattern)
    by_file: dict[tuple[str, str], list[Hit]] = {}
    for hit in hits:
        by_file.setdefault((hit.source_id, hit.path), []).append(hit)

    by_source = {s.id: s for s in sources}
    candidates: list[tuple[Loaded, Hit]] = []
    seen_lines: set[tuple[str, int]] = set()
    degraded = 0
    for (source_id, path), file_hits in by_file.items():
        source = by_source.get(source_id)
        if source is None:
            continue
        if file_hits[0].kind == "spill":
            entry, line_no = _resolve_spill_hit(file_hits[0], source)
            if entry is None:
                degraded += 1
                continue
            resolved = [Hit("spill", source_id, file_hits[0].dir_name, path, line_no,
                            file_hits[0].raw_line)]
        else:
            try:
                entry = corpus.load_one(source, Path(path), file_hits[0].dir_name)
            except OSError:
                continue
            resolved = file_hits
        if not scope.matches(entry):
            continue
        for hit in resolved:
            # Keyed on the transcript's own path, not sid: two real transcripts can share a
            # session id (corpus.py's _disambiguate exists precisely because it happens), and
            # search_rows calls corpus.load_one directly rather than corpus.load(), so that
            # disambiguation never runs here. A resolved spill hit shares its transcript's
            # entry.session.path with the jsonl hit for the same result, so this still
            # dedupes the spill/transcript double-match case.
            key = (entry.session.path, hit.line_no)
            if key in seen_lines:
                continue
            seen_lines.add(key)
            candidates.append((entry, hit))

    candidates.sort(key=lambda pair: (pair[0].session.ended_at or "", pair[1].line_no),
                    reverse=True)
    capped = _cap_per_session(candidates, per_session) if per_session else candidates
    total = len(capped)
    survivors = capped[:limit] if limit else capped
    rows = [_build_row(entry, hit, context, pattern) for entry, hit in survivors]
    return rows, degraded, total


def _cap_per_session(candidates: list[tuple[Loaded, Hit]], per_session: int
                     ) -> list[tuple[Loaded, Hit]]:
    """Keep at most ``per_session`` candidates per ``sid``, preserving the incoming
    (newest-first) order — applied after sorting so a session's *best* (most recent-line)
    hits survive, not its first-scanned."""
    seen: dict[str, int] = {}
    kept: list[tuple[Loaded, Hit]] = []
    for entry, hit in candidates:
        sid = entry.session.sid
        n = seen.get(sid, 0)
        if n >= per_session:
            continue
        seen[sid] = n + 1
        kept.append((entry, hit))
    return kept


def _resolve_spill_hit(hit: Hit, source: Source) -> tuple[Loaded | None, int]:
    """Map a ``tool-results/`` hit back to its session and the transcript line to center on.

    Returns ``(None, 0)`` when the sibling transcript is missing, or no longer names the
    ``tool_use_id`` the spill filename carries — a genuinely degraded state, reported by the
    caller rather than dropped without a trace.
    """
    transcript_path = _spill_transcript_path(Path(hit.path))
    if not transcript_path.is_file():
        return None, 0
    try:
        entry = corpus.load_one(source, transcript_path, hit.dir_name)
    except OSError:
        return None, 0
    tool_use_id = Path(hit.path).stem
    tool = next((t for t in entry.session.tools if t.tool_use_id == tool_use_id), None)
    if tool is None or not tool.result_line:
        return None, 0
    return entry, tool.result_line


def _spill_transcript_path(spill_path: Path) -> Path:
    """``<project>/<sid>/tool-results/<file>.txt`` -> ``<project>/<sid>.jsonl``.

    Always returns a path (never ``None``) — it may simply not exist, which the caller checks.
    """
    session_dir = spill_path.parent.parent
    project_dir = session_dir.parent
    return project_dir / f"{session_dir.name}.jsonl"


#: (line, kind, name, text) — text is always the full, uncapped, redacted re-read, never the
#: fixed-width Message.preview/ToolCall.*_preview kept in memory, because a match can lie
#: past where those previews cut off; display bounding happens later, in `_bounded_text`.
_Entry = tuple[int, str, str, str]


def _context_entries(entry: Loaded, center_line: int, context: int) -> list[_Entry]:
    """Every message, tool call, tool result and system event in
    ``[center_line - context, center_line + context]``, with full text.

    Built directly from ``entry.session.messages``/``.tools``/``.sysev`` rather than
    ``query.timeline_rows`` — that function's rows carry only the capped preview, and it has
    no entry at all for a tool *result* line (only the ``tool_use`` line), which is exactly
    where a search hit inside a tool's output lands.
    """
    lo, hi = center_line - context, center_line + context
    entries: list[_Entry] = []
    for m in entry.session.messages:
        if lo <= m.line <= hi:
            entries.append((m.line, "msg", m.role, query.full_message(entry, m)))
    for t in entry.session.tools:
        if lo <= t.line <= hi:
            entries.append((t.line, "tool", t.name, query.full_input(entry, t)))
        if t.result_line and lo <= t.result_line <= hi:
            prefix = f"[{t.err_class}] " if t.is_error else ""
            entries.append((t.result_line, "result", t.name,
                           prefix + query.full_output(entry, t)))
    for s in entry.session.sysev:
        if lo <= s.line <= hi:
            entries.append((s.line, "sys", s.subtype, s.detail))
    entries.sort(key=lambda e: e[0])
    return entries


#: Characters of context shown on each side of a match inside one excerpt entry. A real
#: transcript line averages 2.4 KB and maxes at 51 KB (PLAN.md §2) — without this, a single
#: huge tool result blows the report's whole byte budget on one row (verified: an untrimmed
#: excerpt around a ~40 KB tool result produced `{"hits": 1, "matches": [], "omitted": ...}`
#: under the default 4 KB JSON budget).
_EXCERPT_WINDOW = 200


def _bounded_text(text: str, pattern: re.Pattern[str]) -> str:
    """Bound one entry's text for display: a window around the match if this entry's text
    contains one, otherwise a flat preview — never the full uncapped text.
    """
    match = pattern.search(text)
    if match is None:
        return text if len(text) <= _EXCERPT_WINDOW else text[:_EXCERPT_WINDOW] + "…"
    lo = max(0, match.start() - _EXCERPT_WINDOW)
    hi = min(len(text), match.end() + _EXCERPT_WINDOW)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return prefix + text[lo:hi] + suffix


def _format_excerpt(entries: list[_Entry], center_line: int, pattern: re.Pattern[str]) -> str:
    """Join context entries into one line, marking the hit with ``>>``. Each entry's text is
    bounded by :func:`_bounded_text` so no single entry can dominate the report's byte budget.
    """
    parts = []
    for line, kind, name, text in entries:
        marker = ">>" if line == center_line else "  "
        parts.append(f"{marker}{line} {kind} {name}: {_bounded_text(text, pattern)}".strip())
    return " | ".join(parts)


def _build_row(entry: Loaded, hit: Hit, context: int, pattern: re.Pattern[str]) -> Row:
    """One search-result row: session identity plus a line-anchored, bounded context excerpt.

    A match on a record type ``_context_entries`` doesn't model at all (e.g. a bare
    ``type: "mode"`` record) has no entry exactly at ``hit.line_no``; the excerpt then falls
    back to the raw matched line itself, still bounded to a window around the match.
    """
    entries = _context_entries(entry, hit.line_no, context) if hit.line_no else []
    center = next((e for e in entries if e[0] == hit.line_no), None)
    excerpt = (_format_excerpt(entries, hit.line_no, pattern) if center
               else _bounded_text(redact_text(hit.raw_line), pattern))
    return {
        "sid": entry.session.sid,
        "project": entry.project_key,
        "line": hit.line_no,
        "kind": center[1] if center else "raw",
        "excerpt": excerpt,
    }
