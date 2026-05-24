"""Process-level watcher registry.

Maps `agent_id → WatcherState` for all agents watched by this process.
The BackgroundWorker and KillPendingPoller read this to enumerate live agents.
"""

from __future__ import annotations

import threading
from uuid import UUID

from stasis_agent.watcher_state import WatcherState

_REGISTRY: dict[UUID, WatcherState] = {}
_REGISTRY_LOCK = threading.Lock()


def register_watcher(state: WatcherState) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[state.agent_id] = state


def unregister_watcher(agent_id: UUID) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.pop(agent_id, None)


def get_watcher(agent_id: UUID) -> WatcherState | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(agent_id)


def all_watchers() -> list[WatcherState]:
    """Snapshot of current watchers (safe to iterate without holding the lock)."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def _reset_registry_for_tests() -> None:
    """Test-only helper. Do not call from production code."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
