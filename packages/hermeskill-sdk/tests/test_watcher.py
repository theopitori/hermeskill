"""Tests for WatcherState, the process-level registry, and BackgroundWorker.

The worker tests use a real HermeskillClient over httpx.MockTransport so we
exercise the actual HTTP path without a live server.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from hermeskill.client import HermeskillClient
from hermeskill.policies import resolve_policy
from hermeskill.types import EventType
from hermeskill.watcher import (
    BackgroundWorker,
    WatcherState,
    all_watchers,
    ensure_worker_started,
    get_watcher,
    register_watcher,
    unregister_watcher,
)


def _make_state(name: str = "test") -> WatcherState:
    return WatcherState(agent_id=uuid4(), name=name, policy=resolve_policy("coding-default"))


# --- WatcherState mutators -----------------------------------------------


def test_record_tool_call_updates_loop_buffer() -> None:
    s = _make_state()
    for _ in range(3):
        s.record_tool_call("read_file", {"path": "a.txt"})
    # All three calls have the same signature → all three identical entries.
    assert len(s.loop_signatures) == 3
    assert len(set(s.loop_signatures)) == 1


def test_record_tool_call_different_params_distinct_signatures() -> None:
    s = _make_state()
    s.record_tool_call("read_file", {"path": "a.txt"})
    s.record_tool_call("read_file", {"path": "b.txt"})
    assert len(set(s.loop_signatures)) == 2


def test_record_llm_call_increments_counters_and_cost() -> None:
    s = _make_state()
    s.record_llm_call("claude-haiku-4-5", 100_000, 50_000)
    s.record_llm_call("claude-haiku-4-5", 200_000, 100_000)
    assert s.total_input_tokens == 300_000
    assert s.total_output_tokens == 150_000
    # 0.3M * $1 + 0.15M * $5 = 0.30 + 0.75 = 1.05
    assert s.total_cost_usd == pytest.approx(1.05, rel=1e-9)


def test_drain_events_clears_queue() -> None:
    s = _make_state()
    s.record_tool_call("x", {})
    s.record_tool_call("y", {})
    drained = s.drain_events()
    assert len(drained) == 2
    assert s.drain_events() == []


def test_requeue_events_preserves_order() -> None:
    s = _make_state()
    s.record_tool_call("a", {})
    drained = s.drain_events()
    # Add new event after drain
    s.record_tool_call("b", {})
    s.requeue_events(drained)
    after = s.drain_events()
    # Requeued events come first (LIFO would be a bug for our use case).
    assert after[0].payload["tool"] == "a"
    assert after[1].payload["tool"] == "b"


def test_uptime_monotonic() -> None:
    s = _make_state()
    u1 = s.uptime_seconds()
    u2 = s.uptime_seconds()
    assert u2 >= u1
    assert u1 >= 0


# --- Registry ------------------------------------------------------------


def test_register_get_unregister() -> None:
    s = _make_state()
    assert get_watcher(s.agent_id) is None
    register_watcher(s)
    assert get_watcher(s.agent_id) is s
    assert s in all_watchers()
    unregister_watcher(s.agent_id)
    assert get_watcher(s.agent_id) is None


def test_all_watchers_returns_snapshot() -> None:
    s1, s2 = _make_state("a"), _make_state("b")
    register_watcher(s1)
    register_watcher(s2)
    snap = all_watchers()
    assert len(snap) == 2
    # Mutation of snap doesn't affect registry.
    snap.clear()
    assert len(all_watchers()) == 2


# --- BackgroundWorker -----------------------------------------------------


def _client_capturing(calls: list[dict[str, Any]]) -> HermeskillClient:
    """A HermeskillClient that records every request to `calls`."""

    def handler(req: httpx.Request) -> httpx.Response:
        body: Any = None
        if req.content:
            import json

            body = json.loads(req.content)
        calls.append({"method": req.method, "url": str(req.url), "body": body})
        # Heartbeat response shape
        if req.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200,
                json={
                    "received_at": datetime.now(UTC).isoformat(),
                    "active_grants": [],
                },
            )
        # Events response shape
        if req.url.path.endswith("/events"):
            return httpx.Response(202, json={"accepted": len(body["events"])})
        return httpx.Response(404, json={"detail": "unmatched"})

    return HermeskillClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_worker_singleton_idempotent() -> None:
    calls: list[dict[str, Any]] = []
    client = _client_capturing(calls)
    try:
        w1 = ensure_worker_started(client, heartbeat_interval=10)
        w2 = ensure_worker_started(client, heartbeat_interval=10)
        assert w1 is w2  # singleton
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_worker_heartbeats_all_registered_watchers() -> None:
    calls: list[dict[str, Any]] = []
    client = _client_capturing(calls)
    s1, s2 = _make_state("a"), _make_state("b")
    register_watcher(s1)
    register_watcher(s2)
    try:
        ensure_worker_started(client, heartbeat_interval=0.05)
        await asyncio.sleep(0.15)  # let it tick at least once
    finally:
        await BackgroundWorker.stop()
        await client.aclose()

    hb_calls = [c for c in calls if c["url"].endswith("/heartbeat")]
    hb_paths = {c["url"] for c in hb_calls}
    assert any(str(s1.agent_id) in p for p in hb_paths)
    assert any(str(s2.agent_id) in p for p in hb_paths)


@pytest.mark.asyncio
async def test_worker_drains_pending_events() -> None:
    calls: list[dict[str, Any]] = []
    client = _client_capturing(calls)
    s = _make_state()
    s.record_tool_call("read_file", {"path": "x"})
    s.record_llm_call("claude-haiku-4-5", 10, 5)
    register_watcher(s)
    try:
        ensure_worker_started(client, heartbeat_interval=0.05)
        await asyncio.sleep(0.2)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()

    events_calls = [c for c in calls if c["url"].endswith("/events")]
    assert events_calls, "worker should have posted events"
    total_events_posted = sum(len(c["body"]["events"]) for c in events_calls)
    assert total_events_posted == 2


@pytest.mark.asyncio
async def test_worker_requeues_events_on_failed_post() -> None:
    """If the events POST 5xx's, events should be re-queued for the next tick."""
    posted_attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal posted_attempts
        if req.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200,
                json={"received_at": datetime.now(UTC).isoformat(), "active_grants": []},
            )
        if req.url.path.endswith("/events"):
            posted_attempts += 1
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(404)

    client = HermeskillClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )
    s = _make_state()
    s.record_tool_call("x", {})
    register_watcher(s)
    try:
        ensure_worker_started(client, heartbeat_interval=0.05)
        await asyncio.sleep(0.18)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()

    # Event should still be pending after failed POST attempts.
    remaining = s.drain_events()
    assert len(remaining) == 1
    assert remaining[0].type == EventType.TOOL_CALL
    assert posted_attempts >= 1


@pytest.mark.asyncio
async def test_worker_heartbeat_failure_does_not_crash() -> None:
    """One bad heartbeat must not stop the worker — flaky server, agent keeps running."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/heartbeat"):
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(202, json={"accepted": 0})

    client = HermeskillClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )
    s = _make_state()
    register_watcher(s)
    try:
        ensure_worker_started(client, heartbeat_interval=0.05)
        await asyncio.sleep(0.15)
        # Worker is still alive — it ticked multiple times despite failures.
        assert BackgroundWorker._instance is not None
        assert BackgroundWorker._instance._task is not None
        assert not BackgroundWorker._instance._task.done()
    finally:
        await BackgroundWorker.stop()
        await client.aclose()
