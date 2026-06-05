"""Watcher: per-agent state, process-level registry, shared background worker.

This module owns three coupled concerns:

1. **`WatcherState`** — one per `watch()` call. Holds counters, the loop-detection
   ring buffer, the apoptosis flag (used by M2), the pending events queue,
   the active grants cache (populated by M5 via heartbeat response), and the
   cleanup-hook list (executed by M2's L1 cooperative termination).

2. **Process-level registry** (`_REGISTRY`) — a dict of `agent_id → WatcherState`.
   Bridges the gap between framework adapters (which only know about their
   hook instances) and the background worker (which needs to enumerate all
   live agents). `register_watcher()` / `unregister_watcher()` are the only
   write paths.

3. **`BackgroundWorker`** — exactly **one** asyncio task per Python process,
   not one per agent (TODO #8). On each tick it: heartbeats every registered
   agent, drains their pending event queues, refreshes their grant caches
   (from the heartbeat response). 50 agents = 1 task, not 50.

Lifecycle: `watch()` calls `ensure_worker_started(client)` which is idempotent
— the first call creates the singleton, subsequent calls return it. The
worker stops when the process exits or `BackgroundWorker.stop()` is called
explicitly.

Apoptosis (M2) flips `state.terminate_requested = True`; framework adapters
(e.g. the Hermes plugin) and `hermeskill.checkpoint()` are what actually raise
`HermeskillTerminated`. This module never raises; it only records.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from hermeskill.pricing import cost_for_usage
from hermeskill.types import EventIn, EventType, Policy, SymptomType

if TYPE_CHECKING:
    from hermeskill.apoptosis import Watchdog
    from hermeskill.client import HermeskillClient

logger = logging.getLogger("hermeskill.watcher")

DEFAULT_HEARTBEAT_INTERVAL = 30  # seconds — overridden per-watcher by policy
DEFAULT_KILL_POLL_INTERVAL = 3  # seconds — M4 manual-kill latency budget
EVENT_BATCH_MAX = 500


# --- WatcherState ---------------------------------------------------------


@dataclass
class WatcherState:
    """Per-agent supervision state. One per `watch()` call.

    `policy` is the resolved Policy object (from `hermeskill.policies`),
    not just the name — M2 symptom checks read thresholds and the tool
    allowlist straight off `state.policy.thresholds.*` and
    `state.policy.tool_allowlist`.
    """

    agent_id: UUID
    name: str
    policy: Policy
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.monotonic)

    # Apoptosis flag — set by M2 symptom checks or by manual kill (M4).
    # The actual `HermeskillTerminated` raise happens in framework adapters
    # and at `hermeskill.checkpoint()` sites, not here.
    terminate_requested: bool = False
    terminate_reason: str | None = None
    terminate_kill_event_id: str | None = None
    # Set by `request_termination()` — the moment the kill decision was
    # made. Goes into the death cert's `triggered_at`. Distinct from the
    # cert's `terminated_at` (which is when the agent actually exits).
    terminate_requested_at: datetime | None = None

    # Manual-kill context (M4). Populated by the kill-pending poller
    # *before* it calls `request_termination()` so the cert builder sees
    # both the flag and the operator info atomically from its POV.
    #
    # Shape: {"operator": str | None, "operator_reason": str | None,
    #         "kill_event_id": int}.
    #
    # `None` for auto-kill paths; the cert builder branches on
    # presence/absence rather than on `trigger_type` so a future kill
    # source (programmatic, scheduled) can populate this without a
    # separate enum bit.
    manual_kill: dict[str, Any] | None = None

    # Control-plane connectivity. True when the agent registered in
    # local-only mode because the control plane was unreachable at setup
    # (see HermeskillPlugin.setup). In-process symptom checks still run and
    # the L1 cooperative block directive still enforces; only control-plane-
    # backed features (operator visibility, manual kill, grants, death-cert
    # archival) are
    # unavailable. The agent_id in this mode is a locally-minted UUID the
    # control plane has no record of.
    offline: bool = False

    # Append-only forensic log — populated by `record_shutdown_step()`
    # during the apoptosis sequence (cert built, hook ran, etc.). Goes
    # into the death cert.
    shutdown_log: list[dict[str, Any]] = field(default_factory=list)

    # Append-only symptoms log — every Terminal/Warning the checks fired.
    # Powered by `record_symptom()` (which also queues the same data as
    # an event for live monitoring). Goes into the death cert.
    symptoms_log: list[dict[str, Any]] = field(default_factory=list)

    # threading.Event paired with `terminate_requested`. The M2.4 watchdog
    # thread sleeps on this so it wakes the *instant* apoptosis is requested,
    # not after a polling interval. All apoptosis-flag flips should go through
    # `request_termination()` so this stays consistent — but the watchdog
    # also polls the flag as a defensive fallback in case a caller writes
    # the flag directly.
    _terminate_event: threading.Event = field(default_factory=threading.Event)

    # L2 watchdog (M2.4) — an SDK primitive that cancels the agent's asyncio
    # task from outside its loop. Only meaningful for a framework adapter that
    # runs the agent as a cancellable asyncio task AND arms the watchdog with
    # that loop+task. The Hermes adapter does NOT (Hermes' agent loop is
    # synchronous), so this stays None in the Hermes integration; L1 enforces
    # and ProcessSupervisor is the hard-kill escape hatch. Left here for the
    # tested standalone primitive and any future async-task adapter.
    watchdog: Watchdog | None = None

    # Loop detection ring buffer. Sized from policy in __post_init__ — set
    # to a placeholder here so the dataclass machinery is happy.
    loop_signatures: deque[str] = field(default_factory=deque)

    # Token/cost runaway counters — updated by the framework adapter on each LLM response.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    # Active grants (M5 populates from heartbeat response).
    grants: list[dict[str, Any]] = field(default_factory=list)

    # Pending events queue — appended by callbacks, drained by the worker.
    # Lock protects against concurrent appends from framework adapter callbacks
    # and the async worker.
    _pending_events: list[EventIn] = field(default_factory=list)
    _events_lock: threading.Lock = field(default_factory=threading.Lock)

    # Cleanup hooks — M2's L1 cooperative termination runs these.
    cleanup_hooks: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Resize the ring buffer to match the policy's loop window. Keep any
        # signatures already appended (none, in practice — __post_init__ runs
        # before any record_* call) by re-creating the deque from the iter.
        self.loop_signatures = deque(
            self.loop_signatures,
            maxlen=self.policy.thresholds.loop_window_actions,
        )

    # --- convenience -------------------------------------------------------

    @property
    def policy_name(self) -> str:
        """Backwards-compat alias for callers that just want the name."""
        return self.policy.name

    # --- mutators (thread-safe where needed) ------------------------------

    def record_tool_call(self, tool_name: str, params: Any) -> None:
        """Append a tool_call event + update loop ring buffer."""
        sig = _signature(tool_name, params)
        self.loop_signatures.append(sig)
        self._enqueue(
            EventIn(
                type=EventType.TOOL_CALL,
                payload={"tool": tool_name, "signature": sig},
            )
        )

    def record_llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Update token/cost counters + queue an llm_call event."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        cost = cost_for_usage(model, input_tokens, output_tokens)
        self.total_cost_usd += cost
        self._enqueue(
            EventIn(
                type=EventType.LLM_CALL,
                payload={
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                    "cumulative_cost_usd": self.total_cost_usd,
                },
            )
        )

    def record_lifecycle(self, phase: str, **extra: Any) -> None:
        self._enqueue(
            EventIn(
                type=EventType.LIFECYCLE,
                payload={"phase": phase, **extra},
            )
        )

    def request_termination(
        self,
        reason: str,
        *,
        kill_event_id: str | None = None,
        manual_kill: dict[str, Any] | None = None,
    ) -> None:
        """Flip the apoptosis flag (first-cause wins) and wake the watchdog.

        Idempotent: if already flagged, the reason and `terminate_requested_at`
        are preserved (the death cert needs to show what *first* killed
        the agent, and *when* it was first decided). The watchdog still
        gets re-poked — harmless because it's already running its grace
        timer.

        All apoptosis-flag flips — symptom checks, manual kill (M4),
        external grant revocations — should go through this method so the
        watchdog wakes immediately rather than waiting for its poll, and
        so `terminate_requested_at` is captured for the death cert.

        `manual_kill` carries operator context for M4 kills. It's set
        **inside** this method (rather than by the caller before the
        call) so first-cause-wins covers both fields atomically: an
        auto-kill that lands a microsecond before the poller can't be
        retroactively re-classified as manual.
        """
        if not self.terminate_requested:
            self.terminate_requested = True
            self.terminate_reason = reason
            self.terminate_kill_event_id = kill_event_id
            self.terminate_requested_at = datetime.now(UTC)
            self.manual_kill = manual_kill
            # Record the first shutdown-log step at the moment of decision
            # so the death cert (built later, from this same list) has
            # something to show — even if the kill path is very short
            # (e.g. tool-scope violation that raises immediately from
            # on_tool_start with no other steps in between).
            self.record_shutdown_step(
                "apoptosis_requested",
                reason=reason,
                kill_event_id=kill_event_id,
            )
        # Set the event unconditionally so a watchdog armed *after* the
        # flag was first flipped still gets the signal.
        self._terminate_event.set()

    def record_shutdown_step(
        self,
        step: str,
        *,
        duration_ms: float | None = None,
        **detail: Any,
    ) -> None:
        """Append a structured entry to the shutdown log.

        Each step gets a timestamp + optional duration + free-form detail
        dict. The death cert serializes the list in order — operators
        read it to understand the apoptosis sequence post-mortem.
        """
        self.shutdown_log.append(
            {
                "step": step,
                "at": datetime.now(UTC).isoformat(),
                "duration_ms": duration_ms,
                "detail": detail,
            }
        )

    def record_symptom(
        self,
        symptom: SymptomType,
        severity: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Queue a SYMPTOM event for `hermeskill logs` AND append to symptoms_log
        for the death cert.

        `severity` is `"terminal"` (apoptosis fires) or `"warning"`
        (suppressed by an M5 grant). Both are sent so audit shows what
        *would have* killed the agent even when a grant let it live.

        We write to two sinks because they have different access patterns:
          * Event queue → drained by BackgroundWorker, fans out to
            `/agents/{id}/events`. Live monitoring; ephemeral.
          * `symptoms_log` → kept in memory until the death cert is
            built. Captures the symptom history even if the event POST
            failed.
        """
        record = {
            "symptom": symptom.value,
            "severity": severity,
            "reason": reason,
            "detail": detail or {},
            "at": datetime.now(UTC).isoformat(),
        }
        self.symptoms_log.append(record)
        self._enqueue(
            EventIn(
                type=EventType.SYMPTOM,
                payload={
                    "symptom": symptom.value,
                    "severity": severity,
                    "reason": reason,
                    "detail": detail or {},
                },
            )
        )

    def _enqueue(self, event: EventIn) -> None:
        with self._events_lock:
            self._pending_events.append(event)

    def drain_events(self) -> list[EventIn]:
        """Atomically take the pending events and return them."""
        with self._events_lock:
            events = self._pending_events
            self._pending_events = []
        return events

    def requeue_events(self, events: list[EventIn]) -> None:
        """Prepend events back onto the queue (e.g. after a failed POST)."""
        with self._events_lock:
            self._pending_events = events + self._pending_events

    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic


def _signature(tool_name: str, params: Any) -> str:
    """Stable signature for loop detection: tool_name + SHA1(canonical-JSON of params)."""
    try:
        canonical = json.dumps(params, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(params)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}|{digest}"


# --- module-level registry ------------------------------------------------

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


# --- BackgroundWorker (TODO #8: one per process, not one per agent) ------


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
        client: HermeskillClient,
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
        self._task = loop.create_task(self._run(), name="hermeskill-background-worker")

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
    client: HermeskillClient,
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


# --- M4 kill-pending poller (sibling singleton) --------------------------


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
        client: HermeskillClient,
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
        self._task = loop.create_task(self._run(), name="hermeskill-kill-poller")

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
    client: HermeskillClient,
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
