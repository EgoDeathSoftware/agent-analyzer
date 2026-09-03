"""Secret redaction for transcript text on its way into a report.

Transcripts routinely contain API keys, bearer tokens and connection strings. Previews are the
only transcript text that leaves this process, so every preview passes through :func:`redact`
as it is built. Since the cache was removed nothing is written to disk at all, which makes this
purely a read-time concern: the risk is a secret reaching a terminal or an agent's context, not
a secret sitting in a derived file.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "<ANTHROPIC_KEY>"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "<API_KEY>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<GITHUB_TOKEN>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<GITHUB_TOKEN>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<AWS_KEY_ID>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "<SLACK_TOKEN>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<JWT>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer <TOKEN>"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[=:]\s*\S{8,}"),
     r"\1=<REDACTED>"),
    (re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:@/]+:[^\s:@/]+@"), "<SCHEME>://<CREDS>@"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "<PRIVATE_KEY>"),
]


def redact(text: str) -> str:
    """Replace recognisable secrets in ``text`` with typed placeholders.

    Args:
        text: Arbitrary transcript text.

    Returns:
        The text with any matched secret shapes substituted.
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def preview(text: str, limit: int) -> str:
    """Redact ``text``, collapse its whitespace and truncate it to ``limit`` characters.

    Args:
        text: Arbitrary transcript text.
        limit: Maximum length of the returned preview.

    Returns:
        A single-line, redacted, length-capped preview. Truncation is marked with a trailing
        ellipsis so callers can tell a short value from a clipped one.
    """
    # Only the leading slice can ever surface in a `limit`-character preview, so redacting the
    # rest is pure waste — tool output routinely runs to hundreds of KB, and running 11 regexes
    # over all of it was most of a corpus parse. The margin covers whitespace collapsing eating
    # into the budget (e.g. heavily indented text); it can only shrink a prefix, never grow it.
    flat = " ".join(redact(text[: limit * 8]).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"
