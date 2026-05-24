"""Kill-pending poller singleton (M4).

Polls `GET /kills/pending` on a short interval and triggers cooperative
termination for any manual kills that land in the local registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, ClassVar

from stasis_agent.watcher_registry import get_watcher
from stasis_agent.watcher_state import DEFAULT_KILL_POLL_INTERVAL

if TYPE_CHECKING:
    from stasis_agent.client import StasisClient

logger = logging.getLogger("stasis_agent.watcher")


class KillPendingPoller:
    """The single per-process task that polls `GET /kills/pending`.

    On each tick: ask the control plane for pending **manual** kills
    across all this caller's agents, look each one up in the local
    registry, and trigger cooperative termination. Auto-kills aren't
    surfaced — the SDK originates them, so it already knows.

    Why a sibling and not part of `BackgroundWorker`:
      * Cadence differs (3s vs 30s heartbeat).
      * Failure isolation: a slow heartbeat shouldn't add latency to
        kill delivery, and a hung kill-poll shouldn't starve the event
        drain.
      * Lifecycle is paired anyway — `ensure_worker_started` boots both.
    """

    _instance: ClassVar[KillPendingPoller | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        client: StasisClient,
        *,
        interval: float = DEFAULT_KILL_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._owning_loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def get(cls) -> KillPendingPoller | None:
        with cls._instance_lock:
            return cls._instance

    @classmethod
    async def stop(cls) -> None:
        with cls._instance_lock:
            inst = cls._instance
            cls._instance = None
        if inst is not None:
            await inst._stop_internal()

    def _start(self) -> None:
        loop = asyncio.get_running_loop()
        self._owning_loop = loop
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._run(), name="stasis-kill-poller")

    async def _run(self) -> None:
        assert self._stop_event is not None
        logger.debug("KillPendingPoller started (interval=%ds)", self._interval)
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._interval
                    )
                    break  # stop requested
                except TimeoutError:
                    pass
                await self._tick()
        except asyncio.CancelledError:
            logger.debug("KillPendingPoller cancelled")
            raise

    async def _tick(self) -> None:
        try:
            pending = await self._client.list_pending_kills()
        except Exception:
            # Network blip, server down, etc. — log and try again next
            # tick. Customer's agent process must NOT die just because
            # the control plane is unreachable.
            logger.exception("kill-pending poll failed")
            return
        if not pending:
            return
        for entry in pending:
            state = get_watcher(entry.agent_id)
            if state is None:
                # Agent isn't watched by this process — could be a
                # different worker holding it, or it died already. The
                # server will eventually mark it ZOMBIE (sweeper,
                # deferred) or someone else will pick it up.
                continue
            if state.terminate_requested:
                # Already being killed (auto symptom got there first, or
                # we delivered this on a prior tick). Skip — first-cause
                # wins, and request_termination wouldn't overwrite the
                # auto context anyway.
                continue
            # request_termination owns the atomicity: it writes the flag,
            # reason, manual_kill, and shutdown_log step under one logical
            # transition. An auto-symptom landing concurrently still wins
            # the flag race — both branches go through the same gate.
            state.request_termination(
                f"manual kill: {entry.operator_reason or entry.trigger_reason}",
                kill_event_id=str(entry.kill_event_id),
                manual_kill={
                    "operator": entry.operator,
                    "operator_reason": entry.operator_reason,
                    "kill_event_id": entry.kill_event_id,
                },
            )

    async def _stop_internal(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


def ensure_kill_poller_started(
    client: StasisClient,
    *,
    interval: float = DEFAULT_KILL_POLL_INTERVAL,
) -> KillPendingPoller:
    """Start the per-process kill poller if not already running. Idempotent."""
    with KillPendingPoller._instance_lock:
        existing = KillPendingPoller._instance
        if existing is not None:
            return existing
        poller = KillPendingPoller(client, interval=interval)
        poller._start()
        KillPendingPoller._instance = poller
        return poller
