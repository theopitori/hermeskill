"""Test isolation for the LangGraph adapter suite.

The watcher registry and the ``BackgroundWorker`` are process-level singletons.
Without a reset between tests, ``all_watchers()[0]`` returns the *first* watcher
ever registered (a stale state from an earlier test), so a flag set on
"the current" watcher lands on the wrong object. This autouse fixture clears
both before and after each test so every test sees a clean process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from caspase.watcher import BackgroundWorker, _reset_registry_for_tests


@pytest_asyncio.fixture(autouse=True)
async def _clean_watcher_state() -> AsyncIterator[None]:
    await BackgroundWorker.stop()
    _reset_registry_for_tests()
    yield
    await BackgroundWorker.stop()
    _reset_registry_for_tests()
