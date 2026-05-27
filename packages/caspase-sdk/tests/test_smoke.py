"""Smoke tests for the SDK scaffold (M0)."""

from __future__ import annotations

import pytest
from caspase import CaspaseError, CaspaseTerminated, __version__


def test_version_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_terminated_is_caspase_error() -> None:
    exc = CaspaseTerminated("loop_detected", kill_event_id="ke_abc")
    assert isinstance(exc, CaspaseError)
    assert exc.reason == "loop_detected"
    assert exc.kill_event_id == "ke_abc"


def test_checkpoint_noop_without_watcher() -> None:
    from caspase import checkpoint
    from caspase.watcher import _reset_registry_for_tests

    _reset_registry_for_tests()
    checkpoint()


def test_checkpoint_raises_when_flag_set() -> None:
    from uuid import uuid4

    from caspase import checkpoint
    from caspase.policies import resolve_policy
    from caspase.watcher import _REGISTRY, WatcherState, _reset_registry_for_tests

    _reset_registry_for_tests()
    state = WatcherState(
        agent_id=uuid4(),
        name="test-checkpoint",
        policy=resolve_policy("coding-default"),
    )
    _REGISTRY[state.agent_id] = state
    try:
        state.request_termination("loop_detected")
        with pytest.raises(CaspaseTerminated, match="loop_detected"):
            checkpoint()
    finally:
        _reset_registry_for_tests()


def test_cli_import() -> None:
    from caspase.cli import app

    assert app is not None
