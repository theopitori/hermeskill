"""Per-model LLM pricing table for cost-runaway tracking.

Hand-maintained. Prices are USD per 1M tokens. Updated by PR.

Per TODO #5, **fail soft**: `cost_for_usage()` returns 0.0 and emits a single
warning for unknown models rather than raising — a missing price entry must
never crash the watcher. Loop / wall-clock / heartbeat checks must keep
running even with broken cost data.

`last_updated` is checked on first lookup; entries older than 30 days log a
warning so we know to refresh.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

logger = logging.getLogger("caspase.pricing")

STALE_THRESHOLD = timedelta(days=30)


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens (input, output)."""

    input_per_mtok: float
    output_per_mtok: float
    last_updated: date


# Reference table — values approximate as of last_updated.
# Keys are normalized model strings (lowercase, no version suffix where stable).
_TABLE: dict[str, Price] = {
    # Anthropic
    "claude-haiku-4-5": Price(1.0, 5.0, date(2026, 5, 1)),
    "claude-sonnet-4-7": Price(3.0, 15.0, date(2026, 5, 1)),
    "claude-opus-4-7": Price(15.0, 75.0, date(2026, 5, 1)),
    # OpenAI
    "gpt-4o-mini": Price(0.15, 0.6, date(2026, 5, 1)),
    "gpt-4o": Price(2.5, 10.0, date(2026, 5, 1)),
    "gpt-5": Price(5.0, 20.0, date(2026, 5, 1)),
}

_warned_models: set[str] = set()
_warned_stale: set[str] = set()


def cost_for_usage(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost. Fails soft on unknown model (returns 0.0, warns once)."""
    key = _normalize(model)
    price = _TABLE.get(key)
    if price is None:
        if model not in _warned_models:
            logger.warning(
                "caspase.pricing: unknown model %r; cost will be 0.0. "
                "Add an entry to pricing.py if cost-runaway checks should cover it.",
                model,
            )
            _warned_models.add(model)
        return 0.0

    age = date.today() - price.last_updated
    if age > STALE_THRESHOLD and key not in _warned_stale:
        logger.warning(
            "caspase.pricing: price for %r is %d days old; consider refreshing.",
            model,
            age.days,
        )
        _warned_stale.add(key)

    return (
        (input_tokens / 1_000_000) * price.input_per_mtok
        + (output_tokens / 1_000_000) * price.output_per_mtok
    )


def _normalize(model: str) -> str:
    """Strip provider prefixes and lowercase. `anthropic:claude-sonnet-4-7` → `claude-sonnet-4-7`."""
    return model.split(":", 1)[-1].strip().lower()
