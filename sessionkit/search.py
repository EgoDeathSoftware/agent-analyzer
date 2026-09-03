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
