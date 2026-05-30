"""Shared pytest fixtures for the SDK test suite."""

import sys
from pathlib import Path
from typing import Any

import pytest

# Make sibling helper modules (e.g. `_supervisor_targets`) importable by bare
# name under `--import-mode=importlib`, and — because the `spawn` start method
# propagates the parent's sys.path — importable in supervisor child processes.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from caspase.watcher import (  # noqa: E402  (after sys.path bootstrap above)
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
