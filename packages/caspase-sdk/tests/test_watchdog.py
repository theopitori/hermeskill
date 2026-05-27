"""Tests for M2.4 — the L2 forced-termination daemon-thread watchdog.

Critical contracts:

1. **Cooperative-wins path** — if the agent task finishes during the
   grace window (i.e. L1 cooperative termination worked), the watchdog
   does NOT escalate. Verified by completing the task ahead of the grace
   timer and asserting no cancellation happens.

2. **Escalation path** — if the task ignores cooperative shutdown
   (simulated with `await asyncio.sleep(LONG)`), the watchdog calls
   `loop.call_soon_threadsafe(task.cancel)` and the task is cancelled
   within `grace + small buffer`. This is the headline contract.

3. **Out-of-loop placement** — the watchdog thread arms with the agent's
   loop but does not itself run inside it. Verified indirectly: even
   when the agent's loop is busy doing `asyncio.sleep` (which doesn't
   block the loop but does block the task), the watchdog still fires.
   (Pure-CPU `while True: pass` is genuinely uncancellable in Python
   without OS signals; the watchdog docstring documents this honest
   limitation. We don't test that case here.)

4. **Idempotent arming** — repeated `arm()` calls don't start multiple
   threads; they just refresh the captured loop/task slots.

5. **Clean teardown** — `stop()` joins the thread within timeout; the
   thread exits when stopped before any kill signal is received.

6. **BackgroundWorker.stop() joins watchdog threads** — cleanly shuts
   down every armed watchdog on worker teardown.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from uuid import uuid4

import pytest
from caspase.apoptosis import Watchdog
from caspase.policies import resolve_policy
from caspase.watcher import (
    BackgroundWorker,
    WatcherState,
    _reset_registry_for_tests,
)

# --- fixtures -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registry_and_worker() -> Any:
    _reset_registry_for_tests()
    BackgroundWorker._instance = None
    yield
    _reset_registry_for_tests()
    BackgroundWorker._instance = None


def _state() -> WatcherState:
    return WatcherState(
        agent_id=uuid4(), name="t", policy=resolve_policy("coding-default")
    )


# --- 1. cooperative-wins path --------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_does_not_escalate_when_task_finishes_in_grace() -> None:
    """The whole point of the grace window: give L1 cooperation a chance.
    If the task finishes naturally before grace expires, escalate is a no-op."""
    state = _state()
    wd = Watchdog(state, grace_seconds=0.5)

    # A short-lived task — completes well before grace expires.
    async def quick() -> str:
        await asyncio.sleep(0.05)
        return "done"

    task = asyncio.create_task(quick())
    wd.arm(asyncio.get_running_loop(), task)

    # Request apoptosis AFTER the task has already finished cooperatively.
    await task  # natural completion
    state.request_termination("test")

    # Wait long enough for the watchdog to evaluate the grace window.
    await asyncio.sleep(0.7)

    # Task completed normally — no CancelledError, no re-raise.
    assert task.done() and not task.cancelled()
    assert task.result() == "done"
    wd.stop()


# --- 2. escalation path (the headline test) ------------------------------


@pytest.mark.asyncio
async def test_watchdog_cancels_task_when_cooperation_ignored() -> None:
    """A task wedged in `await asyncio.sleep(LONG)` ignores cooperative
    shutdown. After grace_seconds, the watchdog calls task.cancel() via
    call_soon_threadsafe; the task wakes with CancelledError on the next
    loop tick."""
    state = _state()
    grace = 0.2
    wd = Watchdog(state, grace_seconds=grace)

    # The "uncooperative" agent: would sleep for 10 minutes if left alone.
    async def stubborn() -> None:
        await asyncio.sleep(600)

    task = asyncio.create_task(stubborn())
    wd.arm(asyncio.get_running_loop(), task)

    # Fire apoptosis.
    t0 = time.monotonic()
    state.request_termination("test")

    # The task should be cancelled within grace + small buffer.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=grace + 1.5)

    elapsed = time.monotonic() - t0
    # Sanity: it took at least the grace window (not an instant cancel).
    assert elapsed >= grace - 0.05, f"escalated too early: {elapsed:.3f}s"
    # And not much longer.
    assert elapsed < grace + 1.5, f"escalation slow: {elapsed:.3f}s"
    assert task.cancelled()

    wd.stop()


@pytest.mark.asyncio
async def test_watchdog_records_escalation_lifecycle_event() -> None:
    """After escalating, the watchdog queues a `watchdog_escalated`
    lifecycle event so the M2.5 death cert can show that L2 fired."""
    state = _state()
    wd = Watchdog(state, grace_seconds=0.1)

    async def stubborn() -> None:
        await asyncio.sleep(600)

    task = asyncio.create_task(stubborn())
    wd.arm(asyncio.get_running_loop(), task)
    state.request_termination("test")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.5)

    # Give the watchdog thread a beat to enqueue the lifecycle event.
    for _ in range(20):
        events = state.drain_events()
        phases = [e.payload.get("phase") for e in events if "phase" in e.payload]
        if "watchdog_escalated" in phases:
            break
        state.requeue_events(events)
        await asyncio.sleep(0.05)
    else:
        pytest.fail("watchdog_escalated lifecycle event was never recorded")

    wd.stop()


# --- 3. out-of-loop placement (defense against same-loop refactor) -------


@pytest.mark.asyncio
async def test_watchdog_runs_in_separate_thread() -> None:
    """The watchdog must run in a non-asyncio thread — same-loop scheduling
    would never fire when the loop is wedged. Verify the daemon thread
    exists and is distinct from the running loop's thread."""
    state = _state()
    wd = Watchdog(state, grace_seconds=10.0)

    async def noop() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(noop())
    wd.arm(asyncio.get_running_loop(), task)

    # Find the watchdog's thread by name.
    wd_threads = [
        t for t in threading.enumerate() if t.name.startswith("caspase-watchdog-")
    ]
    assert len(wd_threads) >= 1
    assert wd_threads[0].is_alive()
    assert wd_threads[0].daemon, "watchdog thread must be daemon so it dies with the process"
    # And it must NOT be the main/event-loop thread.
    assert wd_threads[0] is not threading.main_thread()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    wd.stop()


# --- 4. idempotent arming ------------------------------------------------


@pytest.mark.asyncio
async def test_arm_is_idempotent_starts_only_one_thread() -> None:
    """Calling arm() repeatedly (from on_chain_start on every node) must not
    spawn N threads — just one, refreshing the loop/task slots."""
    state = _state()
    wd = Watchdog(state, grace_seconds=10.0)

    async def noop() -> None:
        await asyncio.sleep(60)

    task1 = asyncio.create_task(noop())
    task2 = asyncio.create_task(noop())
    loop = asyncio.get_running_loop()

    initial_threads = len(
        [t for t in threading.enumerate() if t.name.startswith("caspase-watchdog-")]
    )
    for _ in range(10):
        wd.arm(loop, task1)
    after_first = len(
        [t for t in threading.enumerate() if t.name.startswith("caspase-watchdog-")]
    )
    assert after_first == initial_threads + 1

    # Re-arming with a new task: still no new thread, but the slot is updated.
    wd.arm(loop, task2)
    after_second = len(
        [t for t in threading.enumerate() if t.name.startswith("caspase-watchdog-")]
    )
    assert after_second == initial_threads + 1
    assert wd._task is task2

    task1.cancel()
    task2.cancel()
    for t in (task1, task2):
        with pytest.raises(asyncio.CancelledError):
            await t
    wd.stop()


# --- 5. clean teardown ---------------------------------------------------


@pytest.mark.asyncio
async def test_stop_joins_thread_when_no_kill_fired() -> None:
    """Common case: agent runs to completion without apoptosis. Watchdog
    stop() should make the thread exit promptly."""
    state = _state()
    wd = Watchdog(state, grace_seconds=10.0)

    async def quick() -> None:
        await asyncio.sleep(0.01)

    task = asyncio.create_task(quick())
    wd.arm(asyncio.get_running_loop(), task)
    await task

    wd_thread = next(
        t for t in threading.enumerate() if t.name.startswith("caspase-watchdog-")
    )
    assert wd_thread.is_alive()

    wd.stop(join_timeout=1.0)
    assert not wd_thread.is_alive(), "watchdog thread did not exit on stop()"


def test_stop_without_arm_is_safe() -> None:
    """Watchdog created but never armed (no thread started). stop() must
    not crash trying to join a None thread."""
    state = _state()
    wd = Watchdog(state, grace_seconds=10.0)
    wd.stop()  # should be a no-op


# --- 6. flag flipped before arm (race coverage) --------------------------


@pytest.mark.asyncio
async def test_kill_flag_set_before_arm_still_escalates() -> None:
    """Pathological order: apoptosis requested *before* the watchdog is
    armed. The terminate_event is already set, so the watchdog should
    immediately enter its grace window once armed."""
    state = _state()
    grace = 0.2
    wd = Watchdog(state, grace_seconds=grace)

    # Request termination before arming.
    state.request_termination("test")

    async def stubborn() -> None:
        await asyncio.sleep(600)

    task = asyncio.create_task(stubborn())
    wd.arm(asyncio.get_running_loop(), task)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=grace + 1.5)
    wd.stop()


# --- 7. direct flag write (defensive polling path) -----------------------


@pytest.mark.asyncio
async def test_watchdog_detects_direct_flag_write_via_polling() -> None:
    """If a caller writes `state.terminate_requested = True` directly
    (bypassing request_termination), the threading.Event isn't set —
    but the watchdog also polls the flag, so it still escalates within
    one poll interval + grace.

    This is defense-in-depth — callers SHOULD go through
    request_termination, but bugs happen and the watchdog catches them.
    """
    state = _state()
    grace = 0.15
    wd = Watchdog(state, grace_seconds=grace)

    async def stubborn() -> None:
        await asyncio.sleep(600)

    task = asyncio.create_task(stubborn())
    wd.arm(asyncio.get_running_loop(), task)

    # Direct flag write — does NOT set _terminate_event.
    state.terminate_requested = True
    state.terminate_reason = "direct write"

    # Should still escalate within poll_interval + grace + small buffer.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=grace + 1.5)
    wd.stop()


# --- 8. BackgroundWorker.stop() joins watchdog threads -------------------


@pytest.mark.asyncio
async def test_background_worker_stop_joins_watchdog_threads() -> None:
    """BackgroundWorker.stop() should cleanly shut down every armed watchdog."""
    state = _state()
    wd = Watchdog(state, grace_seconds=10.0)
    state.watchdog = wd

    async def noop() -> None:
        await asyncio.sleep(0.01)

    task = asyncio.create_task(noop())
    wd.arm(asyncio.get_running_loop(), task)
    await task

    from caspase.watcher import register_watcher

    register_watcher(state)

    # Spin up a minimal worker + stop it; assert the watchdog thread exits.
    import httpx
    from caspase.client import CaspaseClient
    from caspase.watcher import ensure_worker_started

    client = CaspaseClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(lambda _r: httpx.Response(202, json={"accepted": 0})),
    )
    try:
        ensure_worker_started(client, heartbeat_interval=10)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()

    wd_threads = [
        t for t in threading.enumerate()
        if t.name == f"caspase-watchdog-{state.agent_id}"
    ]
    # Either fully gone or in the process of joining; assert not alive.
    for t in wd_threads:
        assert not t.is_alive(), "watchdog thread didn't exit on worker stop"
