"""End-to-end tests for the M1.3 endpoints.

Hits the live Postgres via ASGITransport (no real HTTP). Each test cleans up
the agents it creates via the `cleanup_agents` fixture.

NOTE: no `from __future__ import annotations` — see test_smoke.py for why.
"""

import pytest
from httpx import AsyncClient

DEV_DEVELOPER_KEY = "sk_dev_developer_local_only_do_not_ship"
DEV_HEADERS = {"Authorization": f"Bearer {DEV_DEVELOPER_KEY}"}


# --- registration + fleet -------------------------------------------------


@pytest.mark.asyncio
async def test_register_agent_returns_id(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    r = await client.post(
        "/agents",
        json={"name": "test-agent-register", "policy_name": "coding-default"},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["policy_name"] == "coding-default"
    assert "agent_id" in body
    assert "registered_at" in body
    cleanup_agents.append(body["agent_id"])


@pytest.mark.asyncio
async def test_register_requires_auth(client: AsyncClient) -> None:
    r = await client.post(
        "/agents",
        json={"name": "x", "policy_name": "y"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_fleet_lists_my_agents(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    # Register two
    for n in ("fleet-test-A", "fleet-test-B"):
        r = await client.post(
            "/agents",
            json={"name": n, "policy_name": "coding-default"},
            headers=DEV_HEADERS,
        )
        cleanup_agents.append(r.json()["agent_id"])

    r = await client.get("/agents", headers=DEV_HEADERS)
    assert r.status_code == 200, r.text
    names = {a["name"] for a in r.json()}
    assert {"fleet-test-A", "fleet-test-B"} <= names


@pytest.mark.asyncio
async def test_get_agent_404_for_unknown(client: AsyncClient) -> None:
    r = await client.get(
        "/agents/00000000-0000-0000-0000-000000000000",
        headers=DEV_HEADERS,
    )
    assert r.status_code == 404


# --- heartbeats -----------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_updates_last_heartbeat_at(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    r = await client.post(
        "/agents",
        json={"name": "hb-test", "policy_name": "coding-default"},
        headers=DEV_HEADERS,
    )
    aid = r.json()["agent_id"]
    cleanup_agents.append(aid)

    # Before heartbeat: null
    r = await client.get(f"/agents/{aid}", headers=DEV_HEADERS)
    assert r.json()["last_heartbeat_at"] is None

    # After heartbeat: set
    r = await client.post(
        f"/agents/{aid}/heartbeat",
        json={"uptime_seconds": 42.5},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["active_grants"] == []
    assert "received_at" in r.json()

    r = await client.get(f"/agents/{aid}", headers=DEV_HEADERS)
    assert r.json()["last_heartbeat_at"] is not None


# --- events ingest + query ------------------------------------------------


@pytest.mark.asyncio
async def test_events_ingest_and_query(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    r = await client.post(
        "/agents",
        json={"name": "events-test", "policy_name": "coding-default"},
        headers=DEV_HEADERS,
    )
    aid = r.json()["agent_id"]
    cleanup_agents.append(aid)

    # Post a small batch
    batch = {
        "events": [
            {"type": "tool_call", "payload": {"tool": "read_file", "path": "a.txt"}},
            {"type": "tool_call", "payload": {"tool": "read_file", "path": "b.txt"}},
            {"type": "llm_call", "payload": {"model": "claude-haiku", "input_tokens": 100}},
        ]
    }
    r = await client.post(f"/agents/{aid}/events", json=batch, headers=DEV_HEADERS)
    assert r.status_code == 202, r.text
    assert r.json() == {"accepted": 3}

    # Read back (default = descending)
    r = await client.get(f"/agents/{aid}/events", headers=DEV_HEADERS)
    assert r.status_code == 200, r.text
    page = r.json()
    assert len(page["events"]) == 3
    assert page["events"][0]["type"] == "llm_call"  # most recent
    assert page["events"][-1]["type"] == "tool_call"


@pytest.mark.asyncio
async def test_events_tailing_with_after_id(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    r = await client.post(
        "/agents",
        json={"name": "tail-test", "policy_name": "coding-default"},
        headers=DEV_HEADERS,
    )
    aid = r.json()["agent_id"]
    cleanup_agents.append(aid)

    # First batch
    await client.post(
        f"/agents/{aid}/events",
        json={"events": [{"type": "tool_call", "payload": {"n": 1}}]},
        headers=DEV_HEADERS,
    )
    r = await client.get(f"/agents/{aid}/events", headers=DEV_HEADERS)
    last_id = r.json()["last_id"]
    assert last_id is not None

    # Add more
    await client.post(
        f"/agents/{aid}/events",
        json={
            "events": [
                {"type": "tool_call", "payload": {"n": 2}},
                {"type": "tool_call", "payload": {"n": 3}},
            ]
        },
        headers=DEV_HEADERS,
    )

    # Poll for events after last_id — ascending order
    r = await client.get(
        f"/agents/{aid}/events",
        params={"after_id": last_id},
        headers=DEV_HEADERS,
    )
    page = r.json()
    assert len(page["events"]) == 2
    assert page["events"][0]["payload"]["n"] == 2
    assert page["events"][1]["payload"]["n"] == 3
    assert page["last_id"] == page["events"][-1]["id"]


@pytest.mark.asyncio
async def test_events_rejects_both_cursors(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    r = await client.post(
        "/agents",
        json={"name": "cursor-test", "policy_name": "coding-default"},
        headers=DEV_HEADERS,
    )
    aid = r.json()["agent_id"]
    cleanup_agents.append(aid)

    r = await client.get(
        f"/agents/{aid}/events",
        params={"before_id": 100, "after_id": 50},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 400
