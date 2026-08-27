"""Model pricing and per-message cost computation.

Rates are USD per token, current as of 2026-08-26. This deliberately does **not** reuse
``server/src/pricing.ts``: that table predates the Claude 5 family, so every ``claude-sonnet-5``
session in the corpus falls through to its Sonnet-4 default, and its Opus row still carries
Opus-4.5-era rates ($15/$75). Costs here are list-price estimates — Claude Code usage is billed
by subscription, so treat the numbers as relative weight, not an invoice.

Cache multipliers follow the documented ratios: a 5-minute cache write costs 1.25x base input,
a cache read 0.1x.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_M = 1_000_000
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

#: Suffixes Claude Code appends to a model id for deployment variants, e.g.
#: ``claude-opus-5[1m]``. The 1M-context variants bill at standard rates (no long-context
#: premium), so the suffix is stripped before lookup rather than priced separately.
_SUFFIX = re.compile(r"\[[^\]]*\]$|-fast$")


@dataclass(frozen=True)
class Rates:
    """Per-token USD rates for one model."""

    input: float
    output: float

    @property
    def cache_write(self) -> float:
        """Rate for tokens written to the 5-minute prompt cache."""
        return self.input * CACHE_WRITE_MULTIPLIER

    @property
    def cache_read(self) -> float:
        """Rate for tokens served from the prompt cache."""
        return self.input * CACHE_READ_MULTIPLIER


# Sonnet 5 carries introductory pricing of $2/$10 through 2026-08-31; the standard $3/$15 is
# used here rather than tracking a promo window that expires within days of writing.
PRICING: dict[str, Rates] = {
    "claude-fable-5": Rates(10.00 / _M, 50.00 / _M),
    "claude-mythos-5": Rates(10.00 / _M, 50.00 / _M),
    "claude-opus-5": Rates(5.00 / _M, 25.00 / _M),
    "claude-opus-4-8": Rates(5.00 / _M, 25.00 / _M),
    "claude-opus-4-7": Rates(5.00 / _M, 25.00 / _M),
    "claude-opus-4-6": Rates(5.00 / _M, 25.00 / _M),
    "claude-opus-4-5": Rates(5.00 / _M, 25.00 / _M),
    "claude-sonnet-5": Rates(3.00 / _M, 15.00 / _M),
    "claude-sonnet-4-6": Rates(3.00 / _M, 15.00 / _M),
    "claude-sonnet-4-5": Rates(3.00 / _M, 15.00 / _M),
    "claude-haiku-4-5": Rates(1.00 / _M, 5.00 / _M),
}

DEFAULT = PRICING["claude-sonnet-5"]

#: Placeholder model ids Claude Code writes for locally-injected messages. They cost nothing
#: and must not be reported as "unpriced", or every corpus looks misconfigured.
NON_BILLABLE = {"<synthetic>", "<none>", ""}

_unknown: set[str] = set()


def normalise(model: str) -> str:
    """Strip deployment-variant suffixes and date stamps from a model id.

    Args:
        model: Raw model string as recorded in a transcript.

    Returns:
        A key suitable for :data:`PRICING` lookup.
    """
    key = _SUFFIX.sub("", (model or "").strip())
    if key.startswith("anthropic."):
        key = key[len("anthropic."):]
    if key in PRICING:
        return key
    # Dated snapshots such as claude-haiku-4-5-20251001 fall back to their alias.
    trimmed = re.sub(r"-\d{8}$", "", key)
    return trimmed


def rates_for(model: str) -> Rates:
    """Look up rates for ``model``, falling back to Sonnet-tier pricing.

    Unknown models are recorded in :func:`unknown_models` so ``sk doctor`` can report them
    instead of silently mispricing a whole corpus.
    """
    if (model or "").strip() in NON_BILLABLE:
        return Rates(0.0, 0.0)
    key = normalise(model)
    found = PRICING.get(key)
    if found is None:
        if key:
            _unknown.add(key)
        return DEFAULT
    return found


def unknown_models() -> list[str]:
    """Model ids seen so far that had no pricing entry."""
    return sorted(_unknown)


def cost(model: str, tok_in: int, tok_out: int, cache_read: int, cache_create: int) -> float:
    """Compute the USD cost of one message's usage.

    Args:
        model: Model id from the transcript.
        tok_in: Uncached input tokens.
        tok_out: Output tokens.
        cache_read: Tokens served from the prompt cache.
        cache_create: Tokens written to the prompt cache.

    Returns:
        Estimated cost in USD.
    """
    r = rates_for(model)
    return (
        tok_in * r.input
        + tok_out * r.output
        + cache_read * r.cache_read
        + cache_create * r.cache_write
    )
