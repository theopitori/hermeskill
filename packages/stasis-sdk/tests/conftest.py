"""Shared pytest fixtures for the SDK test suite."""

from typing import Any

import pytest
from stasis_agent.watcher import (
    BackgroundWorker,
    KillPendingPoller,
    _reset_registry_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Reset the watcher registry and both singleton workers before and after
    each test so they cannot bleed state across test boundaries."""
    _reset_registry_for_tests()
    BackgroundWorker._instance = None
    KillPendingPoller._instance = None
    yield
    _reset_registry_for_tests()
    BackgroundWorker._instance = None
    KillPendingPoller._instance = None
