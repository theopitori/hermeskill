"""SDK-side M4 tests — kill poller, cert manual branch, CLI staged output.

In-process: no live control plane. The poller is driven against a
mock transport that returns hand-crafted `/kills/pending` payloads.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from stasis_agent.apoptosis import build_death_certificate
from stasis_agent.client import StasisClient
from stasis_agent.policies import resolve_policy
from stasis_agent.types import TriggerType
from stasis_agent.watcher import (
    KillPendingPoller,
    WatcherState,
    ensure_kill_poller_started,
    register_watcher,
)


def _state(agent_id: UUID | None = None) -> WatcherState:
    return WatcherState(
        agent_id=agent_id or uuid4(),
        name="t",
        policy=resolve_policy("coding-default"),
    )


# --- 1. cert builder branches on manual_kill -----------------------------


def test_build_cert_marks_manual_when_manual_kill_present() -> None:
    s = _state()
    s.request_termination(
        "manual kill: deploy",
        kill_event_id="42",
        manual_kill={
            "operator": "op-key-uuid",
            "operator_reason": "deploy rollback",
            "kill_event_id": 42,
        },
    )
    cert = build_death_certificate(s)
    assert cert.trigger_type == TriggerType.MANUAL
    assert cert.operator == "op-key-uuid"
    assert cert.operator_reason == "deploy rollback"
    assert cert.trigger_reason.startswith("manual kill")


def test_build_cert_stays_auto_without_manual_kill() -> None:
    """Regression: nothing changes for the auto path."""
    s = _state()
    s.request_termination("loop detected")
    cert = build_death_certificate(s)
    assert cert.trigger_type == TriggerType.AUTO
    assert cert.operator is None
    assert cert.operator_reason is None


# --- 2. request_termination atomicity ------------------------------------


def test_request_termination_first_cause_wins_for_manual_kill() -> None:
    """If an auto-kill landed first, the manual_kill kwarg must not
    overwrite the auto context — first-cause-wins applies to BOTH the
    flag and the operator dict, as one transition."""
    s = _state()
    s.request_termination("loop detected")  # auto first
    s.request_termination(
        "manual kill: late",
        manual_kill={"operator": "op", "operator_reason": "race loser"},
    )
    assert s.manual_kill is None
    assert s.terminate_reason == "loop detected"

    cert = build_death_certificate(s)
    assert cert.trigger_type == TriggerType.AUTO
    assert cert.operator is None


# --- 3. poller delivers pending kills to the registry --------------------


class _MockTransport(httpx.AsyncBaseTransport):
    """Tiny transport that returns canned `/kills/pending` payloads.

    Tracks request count so the test can assert the poller actually
    polled. Returns 200 + the payload set by the most recent
    `.set_pending()` call.
    """

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.calls = 0

    def set_pending(self, payload: list[dict[str, Any]]) -> None:
        self.pending = payload

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        self.calls += 1
        if request.url.path != "/kills/pending":
            return httpx.Response(404, json={"detail": "not in mock"})
        return httpx.Response(200, json=self.pending)


@pytest.mark.asyncio
async def test_poller_triggers_termination_for_pending_kill() -> None:
    agent_id = uuid4()
    state = _state(agent_id)
    register_watcher(state)

    transport = _MockTransport()
    transport.set_pending(
        [
            {
                "agent_id": str(agent_id),
                "kill_event_id": 7,
                "trigger_reason": "manual kill",
                "triggered_at": datetime.now(UTC).isoformat(),
                "operator_reason": "rolling back deploy",
                "operator": "op-key-id",
            }
        ]
    )
    client = StasisClient(
        base_url="http://test", api_key="sk_x", transport=transport
    )

    try:
        # Very short interval so the test moves fast.
        ensure_kill_poller_started(client, interval=0.05)
        # Give the poller two ticks to land.
        for _ in range(40):
            if state.terminate_requested:
                break
            await asyncio.sleep(0.05)
    finally:
        await KillPendingPoller.stop()
        await client.aclose()

    assert state.terminate_requested
    assert state.manual_kill is not None
    assert state.manual_kill["operator_reason"] == "rolling back deploy"
    assert state.manual_kill["kill_event_id"] == 7
    assert state.terminate_kill_event_id == "7"


@pytest.mark.asyncio
async def test_poller_skips_already_terminating_agent() -> None:
    """If an auto-kill flipped the flag first, the poller must NOT
    rewrite manual_kill (first-cause-wins is enforced by
    request_termination, but the poller should also short-circuit so
    the watchdog isn't re-poked unnecessarily)."""
    agent_id = uuid4()
    state = _state(agent_id)
    state.request_termination("loop detected")  # auto, first
    register_watcher(state)

    transport = _MockTransport()
    transport.set_pending(
        [
            {
                "agent_id": str(agent_id),
                "kill_event_id": 99,
                "trigger_reason": "manual kill",
                "triggered_at": datetime.now(UTC).isoformat(),
                "operator_reason": "loses race",
                "operator": "op-key-id",
            }
        ]
    )
    client = StasisClient(
        base_url="http://test", api_key="sk_x", transport=transport
    )

    try:
        ensure_kill_poller_started(client, interval=0.05)
        await asyncio.sleep(0.25)  # let several ticks pass
    finally:
        await KillPendingPoller.stop()
        await client.aclose()

    assert state.manual_kill is None
    assert state.terminate_reason == "loop detected"


@pytest.mark.asyncio
async def test_poller_handles_unknown_agent_id_gracefully() -> None:
    """A pending kill for an agent not in this process's registry must
    not crash the poller — could legitimately belong to another
    worker."""
    transport = _MockTransport()
    transport.set_pending(
        [
            {
                "agent_id": str(uuid4()),  # never registered locally
                "kill_event_id": 1,
                "trigger_reason": "manual kill",
                "triggered_at": datetime.now(UTC).isoformat(),
                "operator_reason": "elsewhere",
                "operator": "op",
            }
        ]
    )
    client = StasisClient(
        base_url="http://test", api_key="sk_x", transport=transport
    )

    try:
        ensure_kill_poller_started(client, interval=0.05)
        await asyncio.sleep(0.2)
        # The mock was called; nothing crashed.
        assert transport.calls >= 1
    finally:
        await KillPendingPoller.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_poller_survives_server_errors() -> None:
    class FailingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            self.calls += 1
            return httpx.Response(500, json={"detail": "boom"})

    transport = FailingTransport()
    client = StasisClient(
        base_url="http://test", api_key="sk_x", transport=transport
    )

    try:
        ensure_kill_poller_started(client, interval=0.05)
        await asyncio.sleep(0.2)
        assert transport.calls >= 2  # kept polling despite errors
    finally:
        await KillPendingPoller.stop()
        await client.aclose()
