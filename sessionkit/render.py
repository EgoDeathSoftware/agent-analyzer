"""Budget-enforced report rendering.

Every report is sized for an agent's context window rather than a screen, so output is capped
in bytes and tables are truncated explicitly. **Truncation is never silent** — a capped table
always ends with a line naming how many rows were dropped and how to see them. A report that
reads as complete when it is not is worse than no report at all.

Tables are rendered as aligned columns rather than Markdown pipe tables: at a 4 KB budget the
pipe scaffolding costs roughly a quarter of the available space, and aligned columns read the
same in a terminal.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

#: Default budgets per output layer of the index / aggregate / excerpt contract.
BUDGET_INDEX_KB = 12.0
BUDGET_AGGREGATE_KB = 4.0
BUDGET_EXCERPT_KB = 8.0

OMIT_HINT = "raise --budget-kb or narrow --since"

#: Per-cell display width in text-mode tables. JSON never truncates a cell — it is consumed by
#: a program, not read into a context window — and ``--full`` bypasses this for the one row
#: being investigated. This is distinct from row-budget truncation: that drops whole rows and
#: announces a count; this shortens one cell's text and marks it the same way redact.preview
#: does, so a caller can always tell a short value from a clipped one.
MAX_CELL_CHARS = 100


class Report:
    """Accumulates report blocks and renders them within a byte budget."""

    def __init__(self, as_json: bool = False, budget_kb: float = BUDGET_AGGREGATE_KB,
                 full: bool = False) -> None:
        """Create a report.

        Args:
            as_json: Emit a JSON document instead of aligned text.
            budget_kb: Hard ceiling on rendered output size, in kilobytes.
            full: Skip per-cell truncation in text mode (JSON is never cell-truncated either
                way). For the one report being investigated in full, not the default.
        """
        self.as_json = as_json
        self.budget = int(budget_kb * 1024)
        self.full = full
        self._blocks: list[dict[str, Any]] = []

    def meta(self, **values: Any) -> "Report":
        """Attach key/value context shown above the first section."""
        self._blocks.append({"type": "meta", "values": values})
        return self

    def section(self, title: str) -> "Report":
        """Start a titled section."""
        self._blocks.append({"type": "section", "title": title})
        return self

    def text(self, body: str) -> "Report":
        """Add a free-text paragraph."""
        self._blocks.append({"type": "text", "body": body})
        return self

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[Any]],
              key: str = "rows") -> "Report":
        """Add a table. Rows beyond the budget are dropped with an explicit notice."""
        self._blocks.append({
            "type": "table", "key": key, "headers": list(headers),
            "rows": [[_cell(c) for c in row] for row in rows],
        })
        return self

    def render(self) -> str:
        """Render the accumulated blocks as text or JSON, respecting the budget."""
        return self._render_json() if self.as_json else self._render_text()

    def _render_json(self) -> str:
        """JSON rendering; tables carry an ``omitted`` count when truncated."""
        doc: dict[str, Any] = {}
        remaining = self.budget
        for block in self._blocks:
            if block["type"] == "meta":
                doc.update(block["values"])
            elif block["type"] == "text":
                doc.setdefault("notes", []).append(block["body"])
            elif block["type"] == "table":
                kept, omitted, used = _fit(block["rows"], remaining, _json_row_size)
                remaining = max(0, remaining - used)
                doc[block["key"]] = [dict(zip(block["headers"], r)) for r in kept]
                if omitted:
                    doc.setdefault("omitted", {})[block["key"]] = omitted
        return json.dumps(doc, indent=2, default=str)

    def _render_text(self) -> str:
        """Aligned-column rendering with per-table truncation notices."""
        out: list[str] = []
        remaining = self.budget
        for block in self._blocks:
            chunk, used = self._render_block(block, remaining)
            if chunk:
                out.append(chunk)
                remaining = max(0, remaining - used)
        return "\n".join(out).rstrip() + "\n"

    def _render_block(self, block: dict[str, Any], remaining: int) -> tuple[str, int]:
        """Render one block, returning the text and the bytes it consumed."""
        if block["type"] == "meta":
            body = "  ".join(f"{k}={v}" for k, v in block["values"].items())
            return (body, len(body))
        if block["type"] == "section":
            body = f"\n## {block['title']}"
            return (body, len(body))
        if block["type"] == "text":
            return (block["body"], len(block["body"]))
        return self._render_table(block, remaining)

    def _render_table(self, block: dict[str, Any], remaining: int) -> tuple[str, int]:
        """Render a table, truncating rows to fit and stating what was dropped."""
        headers: list[str] = block["headers"]
        rows: list[list[str]] = block["rows"]
        if not rows:
            return ("(none)", 6)
        if not self.full:
            rows = [[_truncate_cell(c) for c in row] for row in rows]
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row[: len(widths)]):
                widths[i] = max(widths[i], len(cell))

        line_cost = sum(widths) + 2 * len(widths) + 1
        budget_rows = max(1, (remaining - line_cost * 2) // max(1, line_cost))
        kept = rows[:budget_rows]
        omitted = len(rows) - len(kept)

        lines = [_row(headers, widths), _row(["-" * w for w in widths], widths)]
        lines.extend(_row(r, widths) for r in kept)
        if omitted:
            lines.append(f"… {omitted} more row(s) omitted ({OMIT_HINT})")
        body = "\n".join(lines)
        return (body, len(body))


def _truncate_cell(text: str, limit: int = MAX_CELL_CHARS) -> str:
    """Shorten one cell for text-mode display, marking truncation with a trailing ellipsis."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Render one aligned row."""
    padded = [str(c).ljust(w) for c, w in zip(cells, widths)]
    return "  ".join(padded).rstrip()


def _cell(value: Any) -> str:
    """Coerce a cell to a single-line string."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
    return " ".join(str(value).split())


def _json_row_size(row: Sequence[str]) -> int:
    """Approximate serialised size of one JSON row."""
    return sum(len(c) for c in row) + 16 * len(row)


def _fit(rows: Sequence[Sequence[str]], remaining: int,
         sizer: Any) -> tuple[list[list[str]], int, int]:
    """Take as many rows as fit in ``remaining`` bytes."""
    kept: list[list[str]] = []
    used = 0
    for row in rows:
        cost = sizer(row)
        if used + cost > remaining:
            break
        kept.append(list(row))
        used += cost
    return (kept, len(rows) - len(kept), used)


def human_cost(value: float) -> str:
    """Format a USD amount compactly."""
    return f"${value:,.2f}" if value >= 0.01 else f"${value:.4f}"
