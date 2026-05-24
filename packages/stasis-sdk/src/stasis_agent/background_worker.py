"""BackgroundWorker singleton — per-process heartbeat and event drain.

One asyncio task per Python process heartbeats all registered agents and
drains their pending event queues on each tick. Boots the KillPendingPoller
as a sibling singleton via `ensure_worker_started`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, ClassVar

from stasis_agent.kill_poller import ensure_kill_poller_started
from stasis_agent.watcher_registry import all_watchers
from stasis_agent.watcher_state import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_KILL_POLL_INTERVAL,
    EVENT_BATCH_MAX,
    WatcherState,
)

if TYPE_CHECKING:
    from stasis_agent.client import StasisClient

logger = logging.getLogger("stasis_agent.watcher")


class BackgroundWorker:
    """The single per-process task that heartbeats and drains events for ALL
    watchers in this process.

    Started lazily by `ensure_worker_started(client)`. Subsequent calls are
    no-ops — the singleton is shared. Each tick: heartbeat every watcher,
    drain their pending events.

    Failures are logged but do NOT crash the worker — a flaky control plane
    can't take down the customer's agent process.
    """

    _instance: ClassVar[BackgroundWorker | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        client: StasisClient,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._client = client
        self._interval = heartbeat_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._owning_loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def get(cls) -> BackgroundWorker | None:
        with cls._instance_lock:
            return cls._instance

    @classmethod
    async def stop(cls) -> None:
        """Stop the worker if running. Drains remaining events first."""
        with cls._instance_lock:
            inst = cls._instance
            cls._instance = None
        if inst is not None:
            await inst._stop_and_drain()

    def _start(self) -> None:
        """Start the worker task in the current running loop."""
        loop = asyncio.get_running_loop()
        self._owning_loop = loop
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._run(), name="stasis-background-worker")

    async def _run(self) -> None:
        assert self._stop_event is not None
        logger.debug("BackgroundWorker started (interval=%ds)", self._interval)
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                    break  # stop requested
                except TimeoutError:
                    pass
                await self._tick()
        except asyncio.CancelledError:
            logger.debug("BackgroundWorker cancelled")
            raise
        finally:
            await self._drain_all()

    async def _tick(self) -> None:
        watchers = all_watchers()
        for state in watchers:
            await self._heartbeat_one(state)
            await self._drain_one(state)

    async def _heartbeat_one(self, state: WatcherState) -> None:
        try:
            resp = await self._client.heartbeat(state.agent_id, state.uptime_seconds())
            # M5 will populate active_grants here for SDK-side grant application.
            state.grants = list(resp.active_grants)
        except Exception:
            logger.exception("heartbeat failed for agent %s", state.agent_id)

    async def _drain_one(self, state: WatcherState) -> None:
        events = state.drain_events()
        if not events:
            return
        # Cap each POST at EVENT_BATCH_MAX to keep request size sane.
        for i in range(0, len(events), EVENT_BATCH_MAX):
            chunk = events[i : i + EVENT_BATCH_MAX]
            try:
                await self._client.post_events(state.agent_id, chunk)
            except Exception:
                logger.exception("events POST failed for agent %s; requeueing", state.agent_id)
                state.requeue_events(events[i:])
                return

    async def _drain_all(self) -> None:
        """Last-chance drain on shutdown."""
        for state in all_watchers():
            await self._drain_one(state)

    async def _stop_and_drain(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        # Stop every L2 watchdog so its thread joins cleanly on shutdown.
        # If escalation happened during the agent's run, the lifecycle
        # event was already enqueued and flushed by `_drain_all()` above;
        # stopping after the drain is fine.
        for state in all_watchers():
            if state.watchdog is not None:
                state.watchdog.stop()


def ensure_worker_started(
    client: StasisClient,
    *,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    kill_poll_interval: float = DEFAULT_KILL_POLL_INTERVAL,
) -> BackgroundWorker:
    """Start the per-process worker if not already running. Idempotent.

    Also boots the kill-pending poller (a sibling singleton, M4). The
    poller has its own cadence — heartbeat is 30s by default but kills
    need to land within a few seconds, so folding them together would
    either waste bandwidth or hurt latency.
    """
    with BackgroundWorker._instance_lock:
        existing = BackgroundWorker._instance
        if existing is None:
            worker = BackgroundWorker(client, heartbeat_interval=heartbeat_interval)
            worker._start()
            BackgroundWorker._instance = worker
            existing = worker
    # Kill-pending poller lives outside the BackgroundWorker class so its
    # cadence + failure mode don't entangle with heartbeats.
    ensure_kill_poller_started(client, interval=kill_poll_interval)
    return existing
