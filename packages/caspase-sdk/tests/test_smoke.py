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


def test_checkpoint_not_yet_implemented() -> None:
    from caspase import checkpoint

    with pytest.raises(NotImplementedError):
        checkpoint()


def test_cli_import() -> None:
    from caspase.cli import app

    assert app is not None
