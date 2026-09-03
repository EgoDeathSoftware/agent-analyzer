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

from sessionkit import corpus
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
#: match set before `search_rows` (Task 4) even gets to apply `--per-session`/`--limit`.
_MAX_HITS_PER_FILE = 50


@dataclass
class Hit:
    """One raw-text match, not yet resolved to a session.

    ``line_no`` is the 1-based JSONL line for a ``kind="jsonl"`` hit, or ``0`` for a
    ``kind="spill"`` hit (Task 3) until :func:`search_rows` (Task 4) maps it back to the
    transcript line whose tool call produced the spilled result.
    """

    kind: str
    source_id: str
    dir_name: str
    path: str
    line_no: int
    raw_line: str


def scan_corpus(sources: list[Source], pattern: re.Pattern[str], *,
                max_hits_per_file: int = _MAX_HITS_PER_FILE) -> list[Hit]:
    """Raw substring/regex scan of every reachable source's transcripts.

    No parsing happens here — this is the fast pass PLAN.md §7 Phase 4 measures at a few
    milliseconds full-corpus (SPEC.md §2, `rg` over the whole corpus). Only files that
    match at all get parsed, by :func:`search_rows`.

    Args:
        sources: Candidate sources, as returned by ``sessionkit.sources.discover()``.
        pattern: Compiled query from :func:`compile_query`.
        max_hits_per_file: Stop recording further hits in one file past this count (a large
            file with a very common query still returns quickly); other files keep scanning.

    Returns:
        Every hit found, in ``sources``/``corpus.transcripts`` order.
    """
    hits: list[Hit] = []
    for source in scannable(sources):
        for path, dir_name in corpus.transcripts(source):
            hits.extend(_scan_jsonl(source, path, dir_name, pattern, max_hits_per_file))
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
