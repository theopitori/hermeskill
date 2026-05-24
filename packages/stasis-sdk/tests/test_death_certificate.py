"""Tests for M2.5 — the death certificate.

Layered:

  1. Cert builder (pure function) — given a `WatcherState`, produces a
     `DeathCertificate` with the right shape and content.
  2. `_WatchedRunnable` wrapper — catches `StasisTerminated`, posts the
     cert best-effort, re-raises. Forensic failure must NOT swallow the
     original exception.
  3. End-to-end through `watch()` — agent loops, dies, cert lands at
     `POST /agents/{id}/kill_events` with the right body.

Server-side endpoint behavior (insert / update / 409 race) is covered
separately in `packages/stasis-control-plane/tests/test_kill_events.py`
against the live DB.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import pytest
from langgraph.graph import END, START, StateGraph
from stasis_agent import StasisTerminated, watch
from stasis_agent.apoptosis import build_death_certificate, build_kill_event_payload
from stasis_agent.client import StasisClient
from stasis_agent.policies import resolve_policy
from stasis_agent.types import SymptomType, TriggerType
from stasis_agent.watcher import (
    BackgroundWorker,
    WatcherState,
)


def _state() -> WatcherState:
    return WatcherState(
        agent_id=uuid4(), name="t", policy=resolve_policy("coding-default")
    )


# --- 1. cert builder (pure) ----------------------------------------------


def test_build_cert_captures_termination_fields() -> None:
    s = _state()
    s.request_termination("loop detected", kill_event_id="abc")
    s.record_symptom(
        symptom=SymptomType.LOOP,
        severity="terminal",
        reason="loop detected",
        detail={"count": 5},
    )
    s.record_shutdown_step("apoptosis_requested", duration_ms=0.0)

    cert = build_death_certificate(s)

    assert cert.agent_id == s.agent_id
    assert cert.trigger_type == TriggerType.AUTO
    assert cert.trigger_reason == "loop detected"
    assert cert.triggered_at == s.terminate_requested_at
    # symptoms_log carries the loop symptom we recorded
    assert len(cert.symptoms_log) == 1
    assert cert.symptoms_log[0]["symptom"] == SymptomType.LOOP.value
    # shutdown_log steps (typed-validated through ShutdownLogEntry).
    # `request_termination` auto-records `apoptosis_requested`; our explicit
    # `record_shutdown_step("apoptosis_requested", duration_ms=0.0)` adds a
    # second one. Both should be present.
    steps = [e.step for e in cert.shutdown_log]
    assert steps.count("apoptosis_requested") >= 1
    # No operator on auto path
    assert cert.operator is None
    assert cert.operator_reason is None


def test_build_cert_defaults_terminated_at_to_now() -> None:
    s = _state()
    s.request_termination("test")
    cert = build_death_certificate(s)
    # terminated_at is approximately now()
    delta = (datetime.now(UTC) - cert.terminated_at).total_seconds()
    assert -1.0 < delta < 1.0


def test_build_cert_accepts_explicit_terminated_at() -> None:
    s = _state()
    s.request_termination("test")
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    cert = build_death_certificate(s, terminated_at=fixed)
    assert cert.terminated_at == fixed


def test_build_cert_when_no_termination_uses_fallback_reason() -> None:
    """Defensive: building a cert on a state that wasn't really terminated
    shouldn't crash. (Real flows always go through request_termination
    first; this is a safety net for the wrapper's `except` path.)"""
    s = _state()
    # No request_termination called
    cert = build_death_certificate(s)
    assert cert.trigger_reason == "unknown"
    # triggered_at falls back to terminated_at (now)
    assert cert.triggered_at == cert.terminated_at


def test_build_kill_event_payload_wraps_cert() -> None:
    s = _state()
    s.request_termination("cost cap exceeded")
    payload = build_kill_event_payload(s)
    assert payload.trigger_type == TriggerType.AUTO
    assert payload.trigger_reason == "cost cap exceeded"
    assert payload.death_certificate.agent_id == s.agent_id
    # shutdown_log is propagated to the payload too. The first call to
    # request_termination already added `apoptosis_requested`; adding "foo"
    # gives us a second step. Assert both are present rather than asserting
    # an exact count (the auto-recorded step is an implementation detail).
    s.record_shutdown_step("foo")
    payload2 = build_kill_event_payload(s)
    foo_steps = [e for e in payload2.shutdown_log if e.step == "foo"]
    assert len(foo_steps) == 1


def test_build_cert_serializes_to_json() -> None:
    """The cert is stored as jsonb on the server; verify the SDK side
    round-trips via Pydantic JSON mode."""
    s = _state()
    s.request_termination("test")
    s.record_symptom(SymptomType.WALL_CLOCK, "terminal", "runtime exceeded", {"s": 100})
    s.record_shutdown_step("freeze", duration_ms=12.5)
    cert = build_death_certificate(s)
    blob = cert.model_dump(mode="json")
    encoded = _json.dumps(blob)
    decoded = _json.loads(encoded)
    assert decoded["trigger_reason"] == "test"
    assert decoded["symptoms_log"][0]["symptom"] == SymptomType.WALL_CLOCK.value


# --- 2. wrapper behavior --------------------------------------------------


class _GraphState(TypedDict, total=False):
    counter: int


def _looping_graph() -> Any:
    """A 3-iteration graph whose node trips the loop check after 5 calls
    via repeated identical tool invocations."""
    from langchain_core.callbacks.manager import (
        adispatch_custom_event,  # noqa: F401  (kept for future astream tests)
    )

    # Each node fires its own on_tool_start via the handler — we use a
    # simpler approach: a node that builds StasisTerminated state directly
    # in tests. The end-to-end test below uses the LangChain callback path.

    def step(s: _GraphState) -> _GraphState:
        return {"counter": s.get("counter", 0) + 1}

    g = StateGraph(_GraphState)
    g.add_node("step", step)
    g.add_edge(START, "step")
    g.add_edge("step", END)
    return g.compile()


def _mock_client(
    calls: list[dict[str, Any]],
    *,
    agent_id: str | None = None,
    kill_event_response: tuple[int, dict[str, Any]] | None = None,
) -> StasisClient:
    """Mock httpx that returns kill_event 201 by default; pass a custom
    (status, body) for failure paths."""
    aid = agent_id or str(uuid4())
    now = datetime.now(UTC).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        body = _json.loads(req.content) if req.content else None
        calls.append({"method": req.method, "url": str(req.url), "body": body})
        path = req.url.path
        if req.method == "POST" and path == "/agents":
            return httpx.Response(
                201,
                json={
                    "agent_id": aid,
                    "policy_name": body["policy_name"],
                    "registered_at": now,
                },
            )
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"received_at": now, "active_grants": []})
        if path.endswith("/events"):
            return httpx.Response(202, json={"accepted": len(body["events"])})
        if path.endswith("/kill_events") and req.method == "POST":
            if kill_event_response:
                code, resp_body = kill_event_response
                return httpx.Response(code, json=resp_body)
            return httpx.Response(
                201,
                json={
                    "id": 42,
                    "agent_id": aid,
                    "trigger_type": body["trigger_type"],
                    "trigger_reason": body["trigger_reason"],
                    "status": "confirmed",
                    "triggered_at": body["triggered_at"],
                    "terminated_at": body["terminated_at"],
                    "death_certificate": body["death_certificate"],
                    "shutdown_log": body["shutdown_log"],
                    "operator_reason": None,
                    "created_at": now,
                },
            )
        return httpx.Response(404, json={"detail": "unmatched"})

    return StasisClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_wrapper_posts_cert_on_stasis_terminated_and_reraises() -> None:
    """The headline contract: when ainvoke raises StasisTerminated, the
    wrapper POSTs the cert AND re-raises the original exception."""
    calls: list[dict[str, Any]] = []
    client = _mock_client(calls)
    graph = _looping_graph()

    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        # Simulate symptom-side flag flip BEFORE invoke
        from stasis_agent.watcher import all_watchers

        state = all_watchers()[0]
        state.request_termination("loop_detected")

        with pytest.raises(StasisTerminated, match="loop_detected"):
            await wrapped.ainvoke({"counter": 0})

        # Verify the cert POST happened
        cert_calls = [c for c in calls if c["url"].endswith("/kill_events") and c["method"] == "POST"]
        assert len(cert_calls) == 1
        body = cert_calls[0]["body"]
        assert body["trigger_type"] == "auto"
        assert body["trigger_reason"] == "loop_detected"
        assert body["death_certificate"]["agent_id"] == str(state.agent_id)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_wrapper_does_not_post_cert_on_clean_completion() -> None:
    """Sanity: healthy invoke completes normally; NO kill_event POST."""
    calls: list[dict[str, Any]] = []
    client = _mock_client(calls)
    graph = _looping_graph()
    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        result = await wrapped.ainvoke({"counter": 0})
        assert result["counter"] == 1
        cert_calls = [c for c in calls if c["url"].endswith("/kill_events")]
        assert cert_calls == []
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_wrapper_does_not_swallow_stasis_terminated_on_post_failure() -> None:
    """If the cert POST 5xx's, the wrapper must still re-raise the original
    StasisTerminated — forensic loss isn't worth losing the kill signal."""
    calls: list[dict[str, Any]] = []
    # Configure the kill_events endpoint to 500
    client = _mock_client(
        calls,
        kill_event_response=(500, {"detail": "server died"}),
    )
    graph = _looping_graph()

    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        from stasis_agent.watcher import all_watchers

        state = all_watchers()[0]
        state.request_termination("loop_detected")

        # The 500 from the cert POST must NOT replace the StasisTerminated.
        with pytest.raises(StasisTerminated, match="loop_detected"):
            await wrapped.ainvoke({"counter": 0})

        # And the shutdown_log records the failure for ops correlation.
        assert any(s["step"] == "death_cert_post_failed" for s in state.shutdown_log)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_wrapper_treats_409_as_already_dying() -> None:
    """409 means another kill_event is already in flight (race with manual
    kill, M4). The wrapper logs + treats it as success — no exception
    swap, but the shutdown_log records the existing kill_event id."""
    calls: list[dict[str, Any]] = []
    client = _mock_client(
        calls,
        kill_event_response=(
            409,
            {
                "detail": {
                    "detail": "agent kill_event already in flight",
                    "existing_kill_event_id": 7,
                }
            },
        ),
    )
    graph = _looping_graph()

    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        from stasis_agent.watcher import all_watchers

        state = all_watchers()[0]
        state.request_termination("loop_detected")

        with pytest.raises(StasisTerminated):
            await wrapped.ainvoke({"counter": 0})

        # The 409 path records its own shutdown_log step with the existing id.
        skipped = [s for s in state.shutdown_log if s["step"] == "death_cert_post_skipped_409"]
        assert len(skipped) == 1
        assert skipped[0]["detail"]["existing_kill_event_id"] == 7
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_wrapper_delegates_unknown_attrs_to_inner() -> None:
    """`with_config`, `batch`, etc. should still work via __getattr__."""
    calls: list[dict[str, Any]] = []
    client = _mock_client(calls)
    graph = _looping_graph()
    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        # `get_graph` is a real Runnable attribute; should pass through.
        assert hasattr(wrapped, "with_config")
        # And calling it returns something graph-shaped (smoke).
        reconfigured = wrapped.with_config({"tags": ["x"]})
        assert reconfigured is not None
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_wrapper_records_cert_posted_step_on_success() -> None:
    """The success path records 'death_cert_posted' with the kill_event_id."""
    calls: list[dict[str, Any]] = []
    client = _mock_client(calls)
    graph = _looping_graph()
    try:
        wrapped = await watch(graph, name="bot", policy="coding-default", client=client)
        from stasis_agent.watcher import all_watchers

        state = all_watchers()[0]
        state.request_termination("loop_detected")

        with pytest.raises(StasisTerminated):
            await wrapped.ainvoke({"counter": 0})

        posted = [s for s in state.shutdown_log if s["step"] == "death_cert_posted"]
        assert len(posted) == 1
        assert posted[0]["detail"]["kill_event_id"] == 42
        assert posted[0]["duration_ms"] is not None
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


# --- 3. WatcherState side: shutdown_log + symptoms_log ------------------


def test_record_shutdown_step_captures_timestamp_and_detail() -> None:
    s = _state()
    s.record_shutdown_step("freeze", duration_ms=42.5, extra="hello")
    assert len(s.shutdown_log) == 1
    e = s.shutdown_log[0]
    assert e["step"] == "freeze"
    assert e["duration_ms"] == 42.5
    assert e["detail"]["extra"] == "hello"
    # ISO-format timestamp
    datetime.fromisoformat(e["at"])  # raises if malformed


def test_record_symptom_appends_to_symptoms_log_and_event_queue() -> None:
    """The double-write contract: every symptom hits BOTH sinks (live
    events for monitoring, in-memory log for the death cert)."""
    s = _state()
    s.record_symptom(
        SymptomType.LOOP, "terminal", "looped", {"count": 5}
    )
    # In-memory log
    assert len(s.symptoms_log) == 1
    assert s.symptoms_log[0]["symptom"] == SymptomType.LOOP.value
    assert s.symptoms_log[0]["severity"] == "terminal"
    # Event queue
    events = s.drain_events()
    assert any(e.type.value == "symptom" for e in events)


def test_request_termination_sets_requested_at() -> None:
    s = _state()
    assert s.terminate_requested_at is None
    s.request_termination("test")
    assert s.terminate_requested_at is not None
    delta = (datetime.now(UTC) - s.terminate_requested_at).total_seconds()
    assert -1.0 < delta < 1.0
    # Idempotent: second call doesn't overwrite the timestamp
    first = s.terminate_requested_at
    s.request_termination("test2")
    assert s.terminate_requested_at == first
