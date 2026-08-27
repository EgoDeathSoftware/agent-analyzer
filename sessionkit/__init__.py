"""sessionkit — fleet-wide analysis of Claude Code sessions.

A zero-dependency toolkit that parses Claude Code / opencode transcripts on demand and emits
compact reports sized for an agent's context window rather than a human's screen.

There is no cache, no index and no database: the JSONL corpus is the store of record and a full
parse is ~1.2 s. See PLAN.md for the three-layer output contract (index / aggregate / excerpt)
that every consumer is expected to descend through.
"""

__version__ = "0.1.0"
