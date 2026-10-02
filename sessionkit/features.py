"""Features parsers extract once from full message text, so detectors never re-read files.

Both parsers call these helpers, which keeps Claude Code and pi sessions comparable: the same
leak scan, repetition measure, reference extraction and completion-claim check run over each
harness's full text, not the 200-char preview.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_FENCE = re.compile(r"```.*?```", re.S)
_LEAKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tool_call-xml", re.compile(r"</?tool_call>|\[TOOL_CALLS\]")),
    ("function-tag", re.compile(r"<function=[\w.-]+>|<parameter=[\w.-]+>")),
    ("harmony", re.compile(r"<\|channel\|>[^\n]{0,80}?to=functions\.|<\|call\|>")),
    ("json-call", re.compile(
        r'^\s*\{\s*"name"\s*:\s*"[\w.-]+"\s*,\s*"(?:arguments|parameters)"\s*:', re.M)),
)

#: A *claim* that the work is finished — stricter than classify's completion regex, which
#: also fires on every sentence containing "works" or "ready".
CLAIM_RE = re.compile(
    r"\b(?:all (?:the )?tests? pass(?:ed|es|ing)?|tests? (?:now )?pass(?:es|ing)?"
    r"|(?:is|are|now) (?:done|complete|fixed|working)|successfully"
    r"|task (?:is )?complete|everything (?:works|is working))\b"
    r"|(?m:^\s*(?:all )?done[.!]?\s*$)|✅", re.I)

_URL = re.compile(r"https?://\S+")
_REF_BACKTICK = re.compile(r"`([^`\n]{2,120})`")
_REF_PATH = re.compile(r"(?<![\w/])@?((?:~|\.{1,2})?/?[\w.-]+(?:/[\w.-]+)+/?)")
_REF_FILE = re.compile(
    r"\b[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|md|json|jsonl|ya?ml|toml|sh|rs|go|java|kt|rb|"
    r"c|h|cpp|hpp|cs|txt|cfg|ini|html|css|sql|lock|env)\b")
_MAX_REFS = 30
_NGRAM = 8
_MIN_GRAMS = 50


def split_content(content: Any, tool_block_type: str) -> tuple[str, str, int]:
    """Split an assistant content field into (visible text, reasoning text, tool-call count).

    Args:
        content: A plain string or a list of content blocks.
        tool_block_type: The block ``type`` this harness uses for a tool call
            (``tool_use`` for Claude Code, ``toolCall`` for pi).
    """
    if isinstance(content, str):
        return content, "", 0
    if not isinstance(content, list):
        return "", "", 0
    visible: list[str] = []
    reasoning: list[str] = []
    calls = 0
    for block in content:
        if isinstance(block, str):
            visible.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == tool_block_type:
            calls += 1
        elif kind in ("thinking", "redacted_thinking"):
            reasoning.append(str(block.get("thinking") or ""))
        elif kind == "text":
            visible.append(str(block.get("text") or ""))
    return "\n".join(v for v in visible if v), "\n".join(r for r in reasoning if r), calls


def markup_leak(text: str) -> str:
    """Name the tool-call markup found in plain text, or ``""``.

    Fenced code is ignored: an agent explaining a tool-call format is not leaking one, and a
    JSON call counts only when it starts its own line, not as an example inside prose.
    """
    if not text:
        return ""
    unfenced = _FENCE.sub("", text)
    for name, pattern in _LEAKS:
        if pattern.search(unfenced):
            return name
    return ""


def repeat_ratio(text: str) -> float:
    """``1 - distinct/total`` over word 8-grams; 0.0 below 50 grams (too short to judge)."""
    words = text.split()
    total = len(words) - _NGRAM + 1
    if total < _MIN_GRAMS:
        return 0.0
    distinct = {tuple(words[i:i + _NGRAM]) for i in range(total)}
    return round(1 - len(distinct) / total, 3)


def extract_refs(text: str) -> list[str]:
    """Paths, backticked identifiers and filenames a user message names, first-seen order.

    URLs are excluded — stripped before matching so a path or filename embedded in a URL is
    never mistaken for one the user named directly.
    """
    text_without_urls = _URL.sub("", text)

    found: list[str] = []
    for pattern in (_REF_BACKTICK, _REF_PATH):
        for match in pattern.finditer(text_without_urls):
            _add_ref(found, match.group(1))
    for match in _REF_FILE.finditer(text_without_urls):
        name = match.group(0)
        if not any(r.endswith("/" + name) for r in found):
            _add_ref(found, name)
    return found[:_MAX_REFS]


def _add_ref(found: list[str], raw: str) -> None:
    ref = raw.strip().rstrip(".,:;")
    if ref and ref not in found:
        found.append(ref)


def claims_done(text: str) -> bool:
    """Whether a message claims the work is finished."""
    return bool(text) and CLAIM_RE.search(text) is not None


def parse_ts(ts: str) -> datetime | None:
    """An ISO-8601 timestamp as an aware datetime, or ``None`` if it is empty or unusable."""
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def delta_ms(start: str, end: str) -> int | None:
    """Milliseconds between two ISO-8601 timestamps, or ``None`` if either is unusable."""
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)
