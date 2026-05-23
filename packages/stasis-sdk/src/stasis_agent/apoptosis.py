"""L2 forced-termination watchdog.

L1 (cooperative termination) lives in `langchain.py` — the `_checkpoint`
raise at chain/tool boundaries. It works as long as the agent's event
loop is alive and reaching await points. When it isn't — agent is wedged
inside a sync tool, or just stubbornly ignoring `StasisTerminated` — we
need an out-of-band path that can cancel from outside the loop.

That's L2: **one daemon `threading.Thread` per watched agent**, holding a
reference to the agent's asyncio loop and main `Task`. The thread sleeps
on `state._terminate_event`. When apoptosis fires, it waits the policy's
`cooperative_grace_seconds`, checks whether the task finished on its own
(L1 worked → no escalation), and if not, calls
`loop.call_soon_threadsafe(task.cancel)` — scheduling cancellation from
*outside* the loop, which is the part that defeats the wedged-loop case.

**Why a thread, not an asyncio task.** If we scheduled the L2 timer with
`asyncio.create_task(...)` in the agent's own loop, it would queue
behind whatever's blocking that loop — i.e. behind the very thing it's
trying to interrupt. Same-loop scheduling defeats the entire purpose.
Run as a thread, run outside the loop. *Do not* refactor this back into
the loop in a future cleanup pass — leave this comment as ballast.

**Honest limitation.** `task.cancel()` raises CancelledError at the next
*await point*. If an agent is wedged in pure-Python CPU code (`while
True: pass` inside a sync tool with no awaits anywhere reachable), the
cancellation will not fire — Python provides no portable way to
interrupt a thread mid-bytecode. The watchdog logs the escalation
attempt; in that case the only real recourse is killing the OS process
(operator escalation, M3 webhook fires, M5 grants document the case).
The watchdog still handles the realistic case (async tool wedged on a
slow network call ignoring cooperative shutdown) — which is what the
plan's "blocked-loop test" intends to exercise.

Public surface: `Watchdog(state, grace_seconds)`, `.arm(loop, task)`,
`.stop()`. Idempotent arming — call from `on_chain_start` every time;
the first call starts the thread, later calls just refresh the captured
loop + task in case a new invocation runs in a different task.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from stasis_agent.types import (
    DeathCertificate,
    KillEventIn,
    ShutdownLogEntry,
    TriggerType,
)

if TYPE_CHECKING:
    from stasis_agent.watcher import WatcherState

logger = logging.getLogger("stasis_agent.apoptosis")


class Watchdog:
    """L2 forced-termination thread. One per `WatcherState`."""

    # Polling cadence for the thread's main wait + grace-period loops.
    # Trades responsiveness against wakeup cost; 100ms is plenty fast for
    # human-perceptible kill latency without burning CPU on idle agents.
    _POLL_SECONDS = 0.1

    def __init__(self, state: WatcherState, *, grace_seconds: float) -> None:
        self.state = state
        self.grace_seconds = grace_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[object] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Guards the loop/task slots. Cheap — only touched on arm() + on
        # transitions inside _run().
        self._lock = threading.Lock()
        # True iff we've already issued a call_soon_threadsafe(task.cancel)
        # for this kill; prevents double-cancel on long-grace policies.
        self._escalated = False

    # --- public API -------------------------------------------------------

    def arm(
        self,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task[object],
    ) -> None:
        """Capture the loop + task to watch. Idempotent.

        On first call: starts the daemon thread.
        On later calls: refreshes the loop/task slots (a new ainvoke may
        run in a different task than the previous one).

        Safe to call from any thread, including the agent's own loop.
        """
        with self._lock:
            self._loop = loop
            self._task = task
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name=f"stasis-watchdog-{self.state.agent_id}",
                )
                self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """Signal the thread to exit. Does NOT force-cancel the task.

        Called on agent unregister / process shutdown. The thread joins
        within `join_timeout`; if it doesn't, we log and move on (daemon
        threads die with the process anyway).
        """
        self._stop.set()
        # Poke the terminate_event so a thread blocked on it wakes up to
        # observe the stop flag. (We can't `notify` a threading.Event the
        # same way as a Condition — set() is the only signal mechanism.)
        self.state._terminate_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                logger.warning(
                    "stasis L2 watchdog: thread %s did not join within %.1fs",
                    thread.name,
                    join_timeout,
                )

    # --- thread body ------------------------------------------------------

    def _run(self) -> None:
        """The daemon thread: wait for kill, give grace, escalate.

        Loop structure:
          1. Wait on `_terminate_event` (with timeout so we can poll
             `_stop` and the flag).
          2. When triggered, wait `grace_seconds` for cooperative
             termination — checking `task.done()` periodically to bail
             out early when L1 wins.
          3. If task still alive after grace: escalate via
             `loop.call_soon_threadsafe(task.cancel)`.
          4. Exit. One watchdog = one kill — no re-arm on the same state.
        """
        logger.debug(
            "stasis L2 watchdog armed for agent %s (grace=%.1fs)",
            self.state.agent_id,
            self.grace_seconds,
        )
        try:
            # --- step 1: wait for kill signal -----------------------
            while not self._stop.is_set():
                triggered = self.state._terminate_event.wait(timeout=self._POLL_SECONDS)
                if self._stop.is_set():
                    return
                # Defensive: also check the flag in case a caller wrote
                # it directly without going through request_termination.
                if triggered or self.state.terminate_requested:
                    break
            else:
                return  # stopped before any kill

            # --- step 2: cooperative-grace window -------------------
            deadline = time.monotonic() + self.grace_seconds
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                with self._lock:
                    task = self._task
                if task is not None and task.done():
                    logger.debug(
                        "stasis L2 watchdog: agent %s cooperated, no escalation",
                        self.state.agent_id,
                    )
                    return
                time.sleep(self._POLL_SECONDS)

            # --- step 3: escalate -----------------------------------
            self._escalate()
        except Exception:
            logger.exception("stasis L2 watchdog crashed for agent %s", self.state.agent_id)

    def _escalate(self) -> None:
        with self._lock:
            loop = self._loop
            task = self._task
            already = self._escalated
            self._escalated = True

        if already:
            return
        if loop is None or task is None:
            logger.warning(
                "stasis L2 watchdog: no loop/task captured for agent %s; "
                "cannot escalate (this is the case the docstring's 'honest "
                "limitation' note describes — operator must kill the process)",
                self.state.agent_id,
            )
            return
        if task.done():
            return  # narrowly raced with cooperative completion

        logger.warning(
            "stasis L2 watchdog: cooperative grace (%.1fs) expired for "
            "agent %s; forcing task cancellation",
            self.grace_seconds,
            self.state.agent_id,
        )
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            # Loop already closed — nothing left to cancel against.
            logger.debug(
                "stasis L2 watchdog: loop already closed for agent %s",
                self.state.agent_id,
            )
        # Record a lifecycle event AND a shutdown-log step so the death
        # cert shows the watchdog fired and audit can correlate timings.
        try:
            self.state.record_lifecycle(
                "watchdog_escalated",
                grace_seconds=self.grace_seconds,
            )
            self.state.record_shutdown_step(
                "watchdog_escalated",
                grace_seconds=self.grace_seconds,
            )
        except Exception:
            logger.exception("watchdog: failed to record escalation lifecycle")


# --- death certificate builder + posting ----------------------------------


def build_death_certificate(
    state: WatcherState,
    *,
    terminated_at: datetime | None = None,
) -> DeathCertificate:
    """Snapshot `state` into a forensic death certificate.

    The cert is built at the very end of the death sequence, after L1
    cooperative termination has raised `StasisTerminated` and the
    wrapper has caught it. By then:

      * `state.terminate_requested` is True
      * `state.terminate_reason` is set (first-cause-wins)
      * `state.terminate_requested_at` is the time the decision was made
      * `state.symptoms_log` holds every symptom (terminal + warning) the
        agent saw during its lifetime
      * `state.shutdown_log` holds the structured shutdown steps so far

    `terminated_at` defaults to now() — the moment of cert build, which
    is effectively the moment of death from the SDK's POV.

    The cert intentionally does NOT include `customer_id` / `policy_id` /
    `feedback_url` — those are server-authoritative (the SDK shouldn't
    be in the business of claiming customer ownership; the server fills
    them from the API key and from M3's signed-token machinery).
    """
    now = terminated_at or datetime.now(UTC)
    triggered_at = state.terminate_requested_at or now
    reason = state.terminate_reason or "unknown"

    # M4: branch on `state.manual_kill` rather than `terminate_reason`.
    # The poller writes the dict atomically with the flag flip, so its
    # presence is the authoritative signal that this kill was operator-
    # issued.
    manual = state.manual_kill
    if manual is not None:
        trigger_type = TriggerType.MANUAL
        operator = manual.get("operator")
        operator_reason = manual.get("operator_reason")
    else:
        trigger_type = TriggerType.AUTO
        operator = None
        operator_reason = None

    return DeathCertificate(
        agent_id=state.agent_id,
        triggered_at=triggered_at,
        terminated_at=now,
        trigger_type=trigger_type,
        trigger_reason=reason,
        symptoms_log=list(state.symptoms_log),
        final_state={},  # v2 / cleanup-hook hookpoint
        shutdown_log=[_normalize_step(s) for s in state.shutdown_log],
        operator=operator,
        operator_reason=operator_reason,
    )


def build_kill_event_payload(
    state: WatcherState,
    *,
    terminated_at: datetime | None = None,
) -> KillEventIn:
    """Wrap the death cert into the `POST /agents/{id}/kill_events` body."""
    cert = build_death_certificate(state, terminated_at=terminated_at)
    return KillEventIn(
        trigger_type=cert.trigger_type,
        trigger_reason=cert.trigger_reason,
        triggered_at=cert.triggered_at,
        terminated_at=cert.terminated_at,
        death_certificate=cert,
        shutdown_log=cert.shutdown_log,
    )


def _normalize_step(raw: dict[str, object]) -> ShutdownLogEntry:
    """Coerce a `record_shutdown_step()`-format dict into the typed model.

    Steps are appended to `state.shutdown_log` as plain dicts (cheap
    write path); we type-validate them only when the cert is built.
    """
    at_value = raw.get("at")
    if isinstance(at_value, str):
        at = datetime.fromisoformat(at_value)
    elif isinstance(at_value, datetime):
        at = at_value
    else:
        at = datetime.now(UTC)
    duration_raw = raw.get("duration_ms")
    duration_ms: float | None = (
        None if duration_raw is None else float(duration_raw)  # type: ignore[arg-type]
    )
    detail = raw.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    step_raw = raw.get("step")
    step = str(step_raw) if step_raw is not None else "unknown"
    return ShutdownLogEntry(
        step=step,
        at=at,
        duration_ms=duration_ms,
        detail=detail,
    )
