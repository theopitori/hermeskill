"""Shared fixtures for caspase-hermes tests."""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from caspase.policies import resolve_policy
from caspase.types import Policy, PolicyThresholds
from caspase.watcher import WatcherState


def make_state(policy: Policy | None = None) -> WatcherState:
    return WatcherState(
        agent_id=uuid4(),
        name="test-agent",
        policy=policy or resolve_policy("coding-default"),
    )


def make_policy(**overrides: object) -> Policy:
    base = resolve_policy("coding-default")
    fields = base.thresholds.model_dump()
    fields.update(overrides)
    return base.model_copy(update={"thresholds": PolicyThresholds(**fields)})


def make_ctx() -> MagicMock:
    """Mock Hermes plugin context."""
    ctx = MagicMock()
    ctx.tool_override = MagicMock()
    ctx.register_hook = MagicMock()
    return ctx


@pytest.fixture
def state() -> WatcherState:
    return make_state()


@pytest.fixture
def ctx() -> MagicMock:
    return make_ctx()
