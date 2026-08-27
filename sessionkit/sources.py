"""Discovery of Claude Code / opencode session sources reachable from this process.

Source paths are deliberately *probed* rather than trusted. ``server/config/sources.json`` holds
container-internal paths (``/claude/wsl``) and ``.env`` names the host user's home
(``/home/david/...``) — neither is necessarily reachable by whoever is running sessionkit. Every
candidate is checked, and unreachable ones are kept in the result with ``reachable=False`` so
``sk doctor`` can report what it could *not* see. Silently seeing one of four sources and
reporting a confident total is the failure mode this module exists to prevent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_KEYS = {
    "CLAUDE_DIR_WSL": ("wsl", "claude-code", "single"),
    "CLAUDE_DIR_WINDOWS": ("windows", "claude-code", "single"),
    "AGENT_CLAUDE_ROOT": ("agents", "claude-code", "store-set"),
    "OPENCODE_DATA_DIR": ("opencode", "opencode", "single"),
}


@dataclass
class Source:
    """One place session transcripts are read from.

    Attributes:
        id: Stable identifier; store-set children use ``<parent>:<store>``.
        kind: ``claude-code`` or ``opencode``.
        layout: ``single`` or ``store-set``.
        location: ``host`` or ``container``.
        root: Filesystem root of the source.
        reachable: Whether ``root`` exists and has the expected shape.
        note: Human-readable reason when unreachable, or extra detail.
        parent_id: Owning store-set source, for container children.
        origin: Container provenance from ``.tracker-origin.json``, when present.
    """

    id: str
    kind: str
    layout: str
    location: str
    root: Path
    reachable: bool = False
    note: str = ""
    parent_id: str | None = None
    origin: dict[str, str] = field(default_factory=dict)

    @property
    def projects_dir(self) -> Path:
        """Directory holding per-project transcript folders."""
        return self.root / "projects"


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` env file, ignoring comments and blanks."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_file_candidates() -> list[Path]:
    """Ordered locations to look for the tracker's ``.env``."""
    explicit = os.environ.get("SESSIONKIT_ENV")
    paths = [Path(explicit)] if explicit else []
    paths.append(Path(__file__).resolve().parents[2] / ".env")
    paths.append(Path("/workspace/.env"))
    return paths


def load_env() -> dict[str, str]:
    """Merge the first readable tracker ``.env`` with the real process environment.

    Returns:
        Mapping of env keys to values; ``os.environ`` wins over the file.
    """
    values: dict[str, str] = {}
    for candidate in _env_file_candidates():
        if candidate.is_file():
            values.update(_read_env_file(candidate))
            break
    for key in (*ENV_KEYS, "CLAUDE_DIR"):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def _probe(source: Source) -> Source:
    """Set ``reachable``/``note`` on ``source`` by inspecting the filesystem."""
    if not source.root.exists():
        source.note = "path does not exist"
        return source
    if source.kind == "opencode":
        db = source.root / "opencode.db"
        source.reachable = db.is_file()
        source.note = "" if source.reachable else "no opencode.db"
        return source
    if source.layout == "store-set":
        source.reachable = source.root.is_dir()
        source.note = "" if source.reachable else "not a directory"
        return source
    source.reachable = source.projects_dir.is_dir()
    source.note = "" if source.reachable else "no projects/ subdirectory"
    return source


def read_origin(store: Path, store_name: str) -> dict[str, str]:
    """Read a container store's ``.tracker-origin.json`` marker, or synthesise a fallback.

    Mirrors ``server/src/store-origin.ts``: a marker missing the ``hostWorkspace`` field is
    unusable for cwd rewriting, but its container name is still kept when present.

    Args:
        store: Path to the per-container store directory.
        store_name: Directory name, used to synthesise a fallback.

    Returns:
        Mapping with any of ``container``, ``image``, ``hostWorkspace``.
    """
    marker = store / ".tracker-origin.json"
    data: dict[str, str] = {}
    if marker.is_file():
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {k: str(v) for k, v in raw.items() if isinstance(v, (str, int))}
        except (OSError, json.JSONDecodeError):
            data = {}
    data.setdefault("container", store_name)
    return data


def rewrite_cwd(cwd: str, origin: dict[str, str]) -> str:
    """Map a container-internal cwd onto its real host path.

    Args:
        cwd: The cwd recorded inside the container, typically under ``/workspace``.
        origin: Origin mapping from :func:`read_origin`.

    Returns:
        The host-side path, or ``cwd`` unchanged when no usable mapping exists.
    """
    host = origin.get("hostWorkspace")
    if not host or not cwd:
        return cwd
    if cwd == "/workspace":
        return host
    if cwd.startswith("/workspace/"):
        return host.rstrip("/") + "/" + cwd[len("/workspace/"):]
    return cwd


def _expand_store_set(parent: Source) -> list[Source]:
    """Expand a store-set source into one container child per store directory."""
    children: list[Source] = []
    try:
        entries = sorted(p for p in parent.root.iterdir() if p.is_dir())
    except OSError as exc:
        parent.note = f"unreadable: {exc.strerror}"
        return children
    for store in entries:
        child = Source(
            id=f"{parent.id}:{store.name}",
            kind="claude-code",
            layout="single",
            location="container",
            root=store,
            parent_id=parent.id,
            origin=read_origin(store, store.name),
        )
        children.append(_probe(child))
    return children


def _candidates(env: dict[str, str]) -> list[Source]:
    """Build the unprobed candidate list from env configuration."""
    host_root = Path(env.get("CLAUDE_DIR") or Path.home() / ".claude").expanduser()
    found = [Source(id="host", kind="claude-code", layout="single",
                    location="host", root=host_root)]
    for key, (sid, kind, layout) in ENV_KEYS.items():
        value = env.get(key)
        if value:
            found.append(Source(id=sid, kind=kind, layout=layout,
                                location="host", root=Path(value).expanduser()))
    return found


def discover() -> list[Source]:
    """Find every session source reachable from this process.

    Returns:
        Probed sources including unreachable ones. Store-set parents are replaced by their
        per-container children, matching the tracker's behaviour.
    """
    seen: dict[str, Source] = {}
    for candidate in _candidates(load_env()):
        key = str(candidate.root.resolve()) if candidate.root.exists() else str(candidate.root)
        if key in seen:
            continue
        seen[key] = _probe(candidate)

    resolved: list[Source] = []
    for source in seen.values():
        if source.layout == "store-set":
            resolved.append(source)
            if source.reachable:
                resolved.extend(_expand_store_set(source))
        else:
            resolved.append(source)
    return resolved


def scannable(sources: list[Source]) -> list[Source]:
    """Filter to sources that can actually be ingested (reachable, non-container-parent)."""
    return [s for s in sources if s.reachable and s.layout != "store-set"]
