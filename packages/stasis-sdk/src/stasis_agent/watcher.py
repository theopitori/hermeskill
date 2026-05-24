"""Backwards-compatibility shim — re-exports everything from the watcher sub-modules.

Existing `from stasis_agent.watcher import WatcherState` calls keep working.
The four sub-modules are:
  * watcher_state    — WatcherState dataclass + constants
  * watcher_registry — process-level agent registry
  * kill_poller      — KillPendingPoller singleton
  * background_worker — BackgroundWorker singleton + ensure_worker_started
"""

from __future__ import annotations

from stasis_agent.background_worker import BackgroundWorker, ensure_worker_started
from stasis_agent.kill_poller import KillPendingPoller, ensure_kill_poller_started
from stasis_agent.watcher_registry import (
    _reset_registry_for_tests,
    all_watchers,
    get_watcher,
    register_watcher,
    unregister_watcher,
)
from stasis_agent.watcher_state import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_KILL_POLL_INTERVAL,
    EVENT_BATCH_MAX,
    WatcherState,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_KILL_POLL_INTERVAL",
    "EVENT_BATCH_MAX",
    "BackgroundWorker",
    "KillPendingPoller",
    "WatcherState",
    "_reset_registry_for_tests",
    "all_watchers",
    "ensure_kill_poller_started",
    "ensure_worker_started",
    "get_watcher",
    "register_watcher",
    "unregister_watcher",
]
