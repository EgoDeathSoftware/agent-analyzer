"""The shape every detector returns."""

from __future__ import annotations

from dataclasses import dataclass

#: Cap on the line numbers a finding cites, so one huge loop doesn't flood a report.
MAX_LINES = 8


@dataclass
class Anomaly:
    """One detected problem in a session."""

    kind: str
    detail: str
    count: int
    lines: list[int]
