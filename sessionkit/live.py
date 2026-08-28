"""Live-session discovery.

Claude Code writes ``<claude_root>/sessions/<pid>.json`` for every running session and
removes the file on exit (``PLAN.md`` §3.2.2). The registry is what makes ``sk tail --all``
honest about which of its `interrupted-tool` candidates are actually running right now
versus abandoned — without this filter, every concurrent session shows up as unfinished
work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


def live_sids(source_roots: Iterable[Path]) -> set[str]:
    """Return every sessionId currently listed in a ``sessions/`` registry.

    ``SESSIONKIT_LIVE_SIDS`` (comma-separated) overrides the on-disk read entirely,
    which is what tests use — no real registry needs to exist under the fixture root.
    """
    override = os.environ.get("SESSIONKIT_LIVE_SIDS")
    if override is not None:
        return {sid.strip() for sid in override.split(",") if sid.strip()}
    sids: set[str] = set()
    for root in source_roots:
        registry = Path(root) / "sessions"
        try:
            entries = list(registry.iterdir())
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        for entry in entries:
            if entry.suffix != ".json":
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            sid = data.get("sessionId") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                sids.add(sid)
    return sids
