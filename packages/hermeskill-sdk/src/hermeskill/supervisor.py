"""L3 process supervisor — the honest hard kill.

The in-process watchdog ([`apoptosis.py`](apoptosis.py)) escalates a kill with
``loop.call_soon_threadsafe(task.cancel)``. That cancels the agent's asyncio
**task** at its next ``await`` — which is useless against an agent wedged in
CPU-bound or synchronous code, because such an agent never reaches an await and
Python provides no portable way to interrupt a thread mid-bytecode. That is the
documented "honest limitation" of L1/L2.

L3 closes it by changing the execution model: run the agent in a **child
process** and supervise it from the parent. Because the supervisor lives in a
*separate* process, it can always escalate to an OS-level kill the child cannot
catch or ignore:

    terminate()  →  grace window  →  kill()

On POSIX that is ``SIGTERM`` → (cooperative window) → ``SIGKILL``. On Windows
both map to ``TerminateProcess`` (there is no catchable SIGTERM), so the grace
window is effectively skipped — the first signal is already a hard kill.

Triggers the supervisor enforces from the parent (the cases L1/L2 can't):

  * **wall_clock** — child has run longer than ``wall_clock_seconds``.
  * **heartbeat_loss** — child stopped calling :meth:`Heartbeat.beat` within
    ``heartbeat_timeout_seconds`` (i.e. it wedged). Opt-in: the supervised
    target must accept a :class:`Heartbeat` as its first argument.

Loop / cost / scope detection deliberately stay in-process (`hermeskill.checks`) —
the supervisor's job is the wedge, not re-deriving symptoms the engine already
catches.

**Spawn discipline (Python 3.14 + Windows).** This module always uses the
``spawn`` start method. Under spawn the child re-imports the target's module and
the ``(target, args)`` pair must be picklable, so:

  * the supervised target **must be a module-level function**, and
  * its args must be picklable (a live ``WatcherState`` is *not* — it holds a
    ``threading.Event``; reconstruct state inside the child instead).

:meth:`ProcessSupervisor.run` validates picklability up front and raises a clear
error rather than letting the child crash opaquely.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import pickle
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("hermeskill.supervisor")

# Trigger labels (also used as the death-cert reason prefix).
TRIGGER_COMPLETED = "completed"
TRIGGER_WALL_CLOCK = "wall_clock"
TRIGGER_HEARTBEAT_LOSS = "heartbeat_loss"


class Heartbeat:
    """Liveness signal a supervised agent pings to prove it's still making
    progress. Backed by a shared ``multiprocessing.Value`` so the parent can
    read it across the process boundary.

    The agent calls :meth:`beat` at points where progress happens (a loop
    iteration, a tool boundary). If it stops beating — because it wedged in
    CPU-bound code — the parent sees the timestamp go stale and kills it.
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    def beat(self) -> None:
        """Record 'I am alive' — call this from the supervised agent."""
        self._value.value = time.time()

    def last_beat(self) -> float:
        return float(self._value.value)


@dataclass(slots=True)
class SupervisorResult:
    """Outcome of one supervised run."""

    trigger: str
    killed: bool
    sigkilled: bool
    exit_code: int | None
    duration_seconds: float
    shutdown_steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed_cleanly(self) -> bool:
        return self.trigger == TRIGGER_COMPLETED and not self.killed


class ProcessSupervisor:
    """Supervise a callable running in a child process; hard-kill on a trigger.

    Parameters mirror the policy knobs an L1/L2 watcher already understands, so
    a caller can drive both from the same ``Policy``.
    """

    def __init__(
        self,
        *,
        grace_seconds: float = 5.0,
        wall_clock_seconds: float | None = None,
        heartbeat_timeout_seconds: float | None = None,
        poll_interval: float = 0.05,
        record_step: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._grace = grace_seconds
        self._wall_clock = wall_clock_seconds
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._poll = poll_interval
        self._record_step = record_step
        self._steps: list[dict[str, Any]] = []

    # --- public API -------------------------------------------------------

    def run(
        self,
        target: Callable[..., Any],
        args: Sequence[Any] = (),
        *,
        use_heartbeat: bool = False,
    ) -> SupervisorResult:
        """Spawn ``target(*args)`` in a child process and supervise it.

        If ``use_heartbeat`` (or a ``heartbeat_timeout_seconds`` was set), a
        :class:`Heartbeat` is prepended to the child's args — the target's
        signature must then be ``target(heartbeat, *args)``.
        """
        ctx = mp.get_context("spawn")

        heartbeat: Heartbeat | None = None
        child_args: tuple[Any, ...] = tuple(args)
        want_heartbeat = use_heartbeat or self._heartbeat_timeout is not None
        if want_heartbeat:
            value = ctx.Value("d", time.time())  # 'd' = double (epoch seconds)
            heartbeat = Heartbeat(value)
            child_args = (heartbeat, *child_args)

        self._guard_picklable(target, args)

        process = ctx.Process(target=target, args=child_args, daemon=False)
        start = time.monotonic()
        process.start()
        try:
            trigger = self._monitor(process, start, heartbeat)
            if trigger == TRIGGER_COMPLETED:
                return SupervisorResult(
                    trigger=trigger,
                    killed=False,
                    sigkilled=False,
                    exit_code=process.exitcode,
                    duration_seconds=time.monotonic() - start,
                    shutdown_steps=list(self._steps),
                )
            self._record(
                "supervisor_trigger", {"trigger": trigger, "pid": process.pid}
            )
            sigkilled = self._escalate(process)
            return SupervisorResult(
                trigger=trigger,
                killed=True,
                sigkilled=sigkilled,
                exit_code=process.exitcode,
                duration_seconds=time.monotonic() - start,
                shutdown_steps=list(self._steps),
            )
        finally:
            # Safety net: never leave a child running, even on an exception or
            # a logic bug. This is what keeps a misbehaving supervisor from
            # hanging CI on a `while True` child.
            if process.is_alive():
                process.kill()
                process.join(timeout=5.0)

    # --- internals --------------------------------------------------------

    def _monitor(
        self,
        process: mp.process.BaseProcess,
        start: float,
        heartbeat: Heartbeat | None,
    ) -> str:
        """Poll until the child exits or trips a trigger. Returns the trigger."""
        while True:
            if not process.is_alive():
                return TRIGGER_COMPLETED
            now = time.monotonic()
            if self._wall_clock is not None and (now - start) >= self._wall_clock:
                return TRIGGER_WALL_CLOCK
            if (
                self._heartbeat_timeout is not None
                and heartbeat is not None
                and (time.time() - heartbeat.last_beat()) >= self._heartbeat_timeout
            ):
                return TRIGGER_HEARTBEAT_LOSS
            time.sleep(self._poll)

    def _escalate(self, process: mp.process.BaseProcess) -> bool:
        """terminate() → grace → kill(). Returns True iff SIGKILL was needed.

        On Windows ``terminate()`` is already a hard kill, so the grace loop
        falls through immediately and ``sigkilled`` stays False (the first
        signal did the job).
        """
        self._record("supervisor_sigterm", {"grace_seconds": self._grace})
        process.terminate()

        deadline = time.monotonic() + self._grace
        while time.monotonic() < deadline:
            if not process.is_alive():
                process.join()
                self._record("supervisor_exited_after_sigterm", {})
                return False
            time.sleep(self._poll)

        if not process.is_alive():
            process.join()
            self._record("supervisor_exited_after_sigterm", {})
            return False

        self._record("supervisor_sigkill", {})
        process.kill()
        process.join()
        return True

    def _guard_picklable(self, target: Callable[..., Any], args: Sequence[Any]) -> None:
        try:
            pickle.dumps((target, tuple(args)))
        except Exception as exc:  # pickling raises many distinct types
            raise ValueError(
                "ProcessSupervisor.run target+args must be picklable for the "
                "'spawn' start method: pass a module-level function and "
                f"picklable args (not e.g. a live WatcherState). cause: {exc!r}"
            ) from exc

    def _record(self, step: str, detail: dict[str, Any]) -> None:
        entry = {
            "step": step,
            "at": datetime.now(UTC).isoformat(),
            "detail": detail,
        }
        self._steps.append(entry)
        logger.debug("hermeskill supervisor: %s %s", step, detail)
        if self._record_step is not None:
            try:
                self._record_step(step, detail)
            except Exception:
                logger.exception("supervisor record_step callback failed")
