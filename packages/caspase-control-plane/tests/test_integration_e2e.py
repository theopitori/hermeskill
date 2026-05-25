"""End-to-end M1.9 — the whole SDK→server loop, no mocks.

Runs the actual demo agent against an in-process FastAPI app (via httpx
ASGITransport) backed by your real Postgres. Verifies the full pipeline:

    SDK watch()
        → POST /agents (register)
        → BackgroundWorker (heartbeats + events)
        → POST /agents/{id}/heartbeat
        → POST /agents/{id}/events
        → GET  /agents/{id}/events  (the SDK reading back)

If this test passes, M1 is done.

NOTE: no `from __future__ import annotations` — see test_smoke.py for why.
"""

from typing import Any

import httpx
import pytest
from caspase import watch
from caspase.client import CaspaseClient
from caspase.types import EventType
from caspase.watcher import (
    BackgroundWorker,
    _reset_registry_for_tests,
)
from control_plane.main import app
from sqlalchemy import text

DEV_DEVELOPER_KEY = "sk_dev_developer_local_only_do_not_ship"


@pytest.fixture(autouse=True)
def _isolate_sdk_state() -> Any:
    """Each test starts with a clean SDK registry + no worker singleton."""
    _reset_registry_for_tests()
    BackgroundWorker._instance = None
    yield
    _reset_registry_for_tests()
    BackgroundWorker._instance = None


@pytest.mark.asyncio
async def test_full_register_events_heartbeat_query_loop(
    cleanup_agents: list[str],
) -> None:
    """The full M1 DoD: register an agent, run it, query its events back."""
    transport = httpx.ASGITransport(app=app)
    client = CaspaseClient(
        base_url="http://test",
        api_key=DEV_DEVELOPER_KEY,
        transport=transport,
    )

    from demo.coding_agent.agent import build_graph

    try:
        # --- 1. watch() registers, attaches callback, starts worker ---
        graph = build_graph()
        watched = await watch(
            graph,
            name="e2e-test-agent",
            policy="coding-default",
            metadata={"e2e": True},
            client=client,
        )

        # Verify registration through the same client
        agents = await client.list_agents()
        ours = [a for a in agents if a.name == "e2e-test-agent"]
        assert len(ours) == 1
        agent_id = ours[0].id
        cleanup_agents.append(str(agent_id))

        # --- 2. Invoke the demo graph — fires lifecycle + tool callbacks ---
        result = await watched.ainvoke({"task": "fix the bug"})
        assert result["edits_made"] == 1
        assert result["files_read"] == ["dummy.py"]

        # --- 3. Stop the worker — this drains pending events to the server ---
        await BackgroundWorker.stop()

        # --- 4. Query events back via the same SDK client ---
        page = await client.list_events(agent_id, limit=50)
        # Server returns descending; sort ascending for inspection
        events = sorted(page.events, key=lambda e: e.id)
        types = [e.type for e in events]

        # Must have at minimum: registered lifecycle + chain_start + tool calls
        assert EventType.LIFECYCLE in types, f"missing lifecycle in {types}"
        assert EventType.TOOL_CALL in types, f"missing tool_call in {types}"

        # The "registered" lifecycle event should be in there
        registered_events = [
            e
            for e in events
            if e.type == EventType.LIFECYCLE and e.payload.get("phase") == "registered"
        ]
        assert len(registered_events) == 1

        # Both tool calls (read_file + write_file) should be present
        tool_calls = [e for e in events if e.type == EventType.TOOL_CALL]
        tools = {e.payload.get("tool") for e in tool_calls}
        assert "read_file" in tools
        assert "write_file" in tools

        # --- 5. The fleet query should show us with a non-null last_heartbeat
        # (worker did at least the shutdown drain; heartbeat may or may not have
        # fired depending on interval, so we don't assert on it being non-null
        # — just confirm the GET works and the agent is there).
        single = await client.get_agent(agent_id)
        assert single.id == agent_id
        assert single.name == "e2e-test-agent"

        # --- 6. Tail mode: ask for events after the last id; should be empty ---
        tail = await client.list_events(agent_id, after_id=page.last_id or 0)
        assert tail.events == []
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_e2e_tool_quarantine_via_termination_flag(
    cleanup_agents: list[str],
) -> None:
    """Pre-set the apoptosis flag → invoke fails fast at first checkpoint.

    This proves the L3 'tool quarantine' wiring in the callback handler works
    end-to-end (M2 will be the part that *sets* the flag automatically).
    """
    from caspase import CaspaseTerminated
    from caspase.watcher import all_watchers

    transport = httpx.ASGITransport(app=app)
    client = CaspaseClient(
        base_url="http://test",
        api_key=DEV_DEVELOPER_KEY,
        transport=transport,
    )

    from demo.coding_agent.agent import build_graph

    try:
        graph = build_graph()
        watched = await watch(
            graph,
            name="e2e-quarantine",
            policy="coding-default",
            client=client,
        )

        state = all_watchers()[0]
        cleanup_agents.append(str(state.agent_id))

        # Pretend M2 fired a terminal symptom
        state.terminate_requested = True
        state.terminate_reason = "loop_detected"

        with pytest.raises(CaspaseTerminated, match="loop_detected"):
            await watched.ainvoke({"task": "doesn't matter"})

        # The graph should NOT have called any tools (edit_step never ran).
        # We can verify by checking that no tool_call events landed.
        await BackgroundWorker.stop()
        page = await client.list_events(state.agent_id, limit=50)
        tool_events = [e for e in page.events if e.type == EventType.TOOL_CALL]
        assert tool_events == [], (
            f"flag was set before invoke; expected no tool calls, got {tool_events}"
        )
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_e2e_events_visible_via_direct_db_query(
    cleanup_agents: list[str],
) -> None:
    """Direct DB read alongside the SDK read — confirms persistence.

    Belt-and-suspenders: makes sure the events we POSTed actually live in
    Postgres rows, not just in some FastAPI response cache.
    """
    from control_plane.db.session import SessionLocal

    transport = httpx.ASGITransport(app=app)
    client = CaspaseClient(
        base_url="http://test",
        api_key=DEV_DEVELOPER_KEY,
        transport=transport,
    )

    from demo.coding_agent.agent import build_graph

    try:
        graph = build_graph()
        watched = await watch(
            graph, name="e2e-db-check", policy="coding-default", client=client
        )
        from caspase.watcher import all_watchers

        agent_id = all_watchers()[0].agent_id
        cleanup_agents.append(str(agent_id))

        await watched.ainvoke({"task": "x"})
        await BackgroundWorker.stop()

        async with SessionLocal() as session:
            row_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM events WHERE agent_id = :aid"),
                    {"aid": agent_id},
                )
            ).scalar()
        assert row_count and row_count >= 3, (
            f"expected ≥3 persisted events, got {row_count}"
        )
    finally:
        await BackgroundWorker.stop()
        await client.aclose()
