"""Lifecycle detectors: how a session's calls and subagents ran, stalled or ended."""

from __future__ import annotations

from sessionkit.detectors.base import MAX_LINES, Anomaly
from sessionkit.harness import role
from sessionkit.parse import ParsedSession
from sessionkit.taxonomy import THRESHOLDS


def agent_trouble(session: ParsedSession) -> list[Anomaly]:
    """Killed or never-returning subagents."""
    out = []
    kills = [e.line for e in session.sysev if e.subtype == "agents_killed"]
    if kills:
        out.append(Anomaly("agent-kill", "subagent killed", len(kills), kills[:MAX_LINES]))
    orphans = [c.line for c in session.tools
               if role(session.harness, c.name) == "dispatch" and c.err_class == "no-result"]
    if orphans:
        out.append(Anomaly("orphan-subagent", "Agent spawned, never returned",
                           len(orphans), orphans[:MAX_LINES]))
    return out


def compaction_churn(session: ParsedSession) -> list[Anomaly]:
    """Repeated context compaction — the session outgrew its window more than once."""
    lines = [e.line for e in session.sysev if e.subtype == "compact_boundary"]
    if len(lines) >= THRESHOLDS["compaction_churn"]:
        return [Anomaly("compaction-churn", "context compacted repeatedly", len(lines),
                        lines[:MAX_LINES])]
    return []


def stalls(session: ParsedSession) -> list[Anomaly]:
    """Individual tool calls that ran far longer than the threshold."""
    limit = THRESHOLDS["stall_ms"]
    slow = [c for c in session.tools if c.dur_ms is not None and c.dur_ms > limit]
    return [Anomaly("stall", f"{c.name} took {c.dur_ms // 1000}s", 1, [c.line]) for c in slow]
