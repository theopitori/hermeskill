"""Shared fixtures for hermeskill-hermes tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from hermeskill import certificate, vitals
from hermeskill.policies import resolve_policy
from hermeskill.types import Policy, PolicyThresholds
from hermeskill.watcher import WatcherState


@pytest.fixture(autouse=True)
def _isolate_hermeskill_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the live-vitals and death-cert directories into a tmp path so
    the plugin's best-effort file writes (snapshots, certs) never touch the
    developer's real ``~/.hermeskill`` during the test run."""
    monkeypatch.setattr(vitals, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(certificate, "KILLS_DIR", tmp_path / "kills")


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
    """Mock Hermes plugin context.

    Real Hermes builds this in hermes_cli/plugins.py::PluginContext. We only
    need register_hook for our tests; the plugin no longer touches ctx after
    register() returns (no tool_override path).
    """
    ctx = MagicMock()
    ctx.register_hook = MagicMock()
    return ctx


@pytest.fixture
def state() -> WatcherState:
    return make_state()


@pytest.fixture
def ctx() -> MagicMock:
    return make_ctx()
