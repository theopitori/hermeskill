"""Tests for the ``watch()`` entrypoint and the LangGraph wrapper.

Uses httpx.MockTransport for the registration HTTP call + a real LangGraph
StateGraph so the callback round-trip is exercised end-to-end (without a live
control plane).
"""

import json as _json
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import uuid4

import caspase_langgraph.adapter as watch_mod
import httpx
import pytest
from caspase.client import AuthError, CaspaseClient
from caspase.exceptions import CaspaseTerminated
from caspase.policies import resolve_policy
from caspase.watcher import (
    BackgroundWorker,
    WatcherState,
    all_watchers,
)
from caspase_langgraph import watch
from caspase_langgraph.adapter import with_caspase
from caspase_langgraph.callback import CaspaseCallbackHandler
from langgraph.graph import END, START, StateGraph


class _GraphState(TypedDict, total=False):
    counter: int
    visited: list[str]


def _build_two_node_graph() -> Any:
    """A tiny LangGraph that goes START → step_a → step_b → END."""

    def step_a(state: _GraphState) -> _GraphState:
        return {
            "counter": state.get("counter", 0) + 1,
            "visited": [*state.get("visited", []), "a"],
        }

    def step_b(state: _GraphState) -> _GraphState:
        return {
            "counter": state.get("counter", 0) + 1,
            "visited": [*state.get("visited", []), "b"],
        }

    g = StateGraph(_GraphState)
    g.add_node("step_a", step_a)
    g.add_node("step_b", step_b)
    g.add_edge(START, "step_a")
    g.add_edge("step_a", "step_b")
    g.add_edge("step_b", END)
    return g.compile()


def _client_capturing(calls: list[dict[str, Any]], agent_id: str | None = None) -> CaspaseClient:
    aid = agent_id or str(uuid4())
    now = datetime.now(UTC).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        body = _json.loads(req.content) if req.content else None
        calls.append({"method": req.method, "url": str(req.url), "body": body})
        path = req.url.path
        if req.method == "POST" and path == "/agents":
            return httpx.Response(
                201,
                json={"agent_id": aid, "policy_name": body["policy_name"], "registered_at": now},
            )
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"received_at": now, "active_grants": []})
        if path.endswith("/events"):
            return httpx.Response(202, json={"accepted": len(body["events"])})
        return httpx.Response(404, json={"detail": "unmatched"})

    return CaspaseClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )


# --- watch() public API ---------------------------------------------------


@pytest.mark.asyncio
async def test_watch_registers_and_returns_invokable_graph() -> None:
    calls: list[dict[str, Any]] = []
    agent_id = str(uuid4())
    client = _client_capturing(calls, agent_id=agent_id)
    graph = _build_two_node_graph()

    try:
        wrapped = await watch(
            graph,
            name="bot-v1",
            policy="coding-default",
            metadata={"env": "test"},
            client=client,
        )
        result = await wrapped.ainvoke({"counter": 0, "visited": []})

        # Assertions must run BEFORE BackgroundWorker.stop() since stop()
        # clears the singleton and drains pending events.
        reg = next(c for c in calls if c["url"].endswith("/agents") and c["method"] == "POST")
        assert reg["body"]["name"] == "bot-v1"
        assert reg["body"]["policy_name"] == "coding-default"
        assert reg["body"]["metadata"] == {"env": "test"}

        # The wrapped graph still runs the customer's logic
        assert result["counter"] == 2
        assert result["visited"] == ["a", "b"]

        # A WatcherState was registered with the returned agent_id
        watchers = all_watchers()
        assert len(watchers) == 1
        assert str(watchers[0].agent_id) == agent_id
        assert watchers[0].name == "bot-v1"

        # The BackgroundWorker is running
        assert BackgroundWorker._instance is not None
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_watch_records_registered_lifecycle_event() -> None:
    """The 'registered' lifecycle event reaches the control plane (POSTed by
    the worker on shutdown drain)."""
    calls: list[dict[str, Any]] = []
    client = _client_capturing(calls)
    graph = _build_two_node_graph()
    try:
        await watch(graph, name="bot", policy="coding-default", client=client)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()

    events_calls = [c for c in calls if c["url"].endswith("/events")]
    posted = [
        e for c in events_calls for e in (c["body"]["events"] if c["body"] else [])
    ]
    assert any(
        e["type"] == "lifecycle" and e["payload"].get("phase") == "registered"
        for e in posted
    )


# --- apoptosis flag -> CaspaseTerminated ---------------------------------


@pytest.mark.asyncio
async def test_terminated_flag_raises_at_chain_start() -> None:
    """When terminate_requested is set BEFORE invoke, the next chain boundary raises."""
    client = _client_capturing([])
    graph = _build_two_node_graph()
    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        state = all_watchers()[0]

        # Pretend a symptom check set the apoptosis flag.
        state.request_termination("loop_detected")

        with pytest.raises(CaspaseTerminated, match="loop_detected"):
            await wrapped.ainvoke({"counter": 0, "visited": []})
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_no_termination_when_flag_unset() -> None:
    """Sanity: with flag unset, invoke completes normally (regression guard)."""
    client = _client_capturing([])
    graph = _build_two_node_graph()
    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        result = await wrapped.ainvoke({"counter": 0, "visited": []})
        assert result["counter"] == 2
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


# --- error paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_propagates_registration_error_and_closes_owned_client() -> None:
    """If registration fails, watch() must not leave a half-initialized client."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad key"})

    original_from_config = CaspaseClient.from_config
    closed: list[bool] = []

    def fake_from_config(*args: Any, **kwargs: Any) -> CaspaseClient:
        c = CaspaseClient(
            base_url="http://test",
            api_key="sk_bad",
            transport=httpx.MockTransport(handler),
        )
        original_aclose = c.aclose

        async def tracking_aclose() -> None:
            closed.append(True)
            await original_aclose()

        c.aclose = tracking_aclose  # type: ignore[method-assign]
        return c

    watch_mod.CaspaseClient.from_config = staticmethod(fake_from_config)  # type: ignore[method-assign]
    try:
        with pytest.raises(AuthError):
            await watch(_build_two_node_graph(), name="x", policy="coding-default")
    finally:
        watch_mod.CaspaseClient.from_config = original_from_config  # type: ignore[method-assign]
        await BackgroundWorker.stop()

    assert closed == [True], "watch() must close the client it owned when registration fails"


# --- with_caspase sanity --------------------------------------------------


def test_with_caspase_rejects_uncompiled_input() -> None:
    state = WatcherState(agent_id=uuid4(), name="t", policy=resolve_policy("coding-default"))
    handler = CaspaseCallbackHandler(state)
    with pytest.raises(TypeError, match="compiled"):
        with_caspase("not a runnable", handler)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_with_caspase_attaches_callback_to_runnable() -> None:
    """Without watch(), can manually wire a graph + handler for unit tests."""
    state = WatcherState(agent_id=uuid4(), name="t", policy=resolve_policy("coding-default"))
    handler = CaspaseCallbackHandler(state)
    graph = _build_two_node_graph()
    wrapped = with_caspase(graph, handler)

    await wrapped.ainvoke({"counter": 0, "visited": []})
    events = state.drain_events()
    assert any(e.payload.get("phase") == "chain_start" for e in events)
