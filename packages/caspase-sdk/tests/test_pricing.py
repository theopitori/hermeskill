"""Tests for the per-model pricing table — TODO #5 (fail soft on unknown)."""

import logging

from caspase import pricing


def test_known_model_returns_real_cost() -> None:
    # 1M input + 0M output of haiku at $1/MTok in
    cost = pricing.cost_for_usage("claude-haiku-4-5", 1_000_000, 0)
    assert cost == 1.0


def test_unknown_model_returns_zero_and_warns_once(
    caplog: logging.LogCaptureFixture,
) -> None:
    pricing._warned_models.clear()
    with caplog.at_level(logging.WARNING, logger="caspase.pricing"):
        # First call: warns
        cost1 = pricing.cost_for_usage("totally-fake-model-xyz", 1000, 1000)
        # Second call: silent (already warned)
        cost2 = pricing.cost_for_usage("totally-fake-model-xyz", 5000, 5000)
    assert cost1 == 0.0
    assert cost2 == 0.0
    warn_count = sum(
        1 for r in caplog.records if "totally-fake-model-xyz" in r.message
    )
    assert warn_count == 1, "should warn exactly once per unknown model"


def test_normalize_strips_provider_prefix() -> None:
    assert pricing._normalize("anthropic:claude-sonnet-4-7") == "claude-sonnet-4-7"
    assert pricing._normalize("Claude-Haiku-4-5") == "claude-haiku-4-5"


def test_provider_prefixed_model_resolves() -> None:
    # Provider-prefixed model should still be priced correctly.
    cost = pricing.cost_for_usage("openai:gpt-4o-mini", 1_000_000, 0)
    assert cost == 0.15
