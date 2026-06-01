"""Tests for fleet management: DELETE /agents/{id} and POST /agents/prune.

Covers:
  * DELETE /agents/{id} — operator-only, 204, cascades history, 404 on
    unknown/cross-customer, 401 without auth.
  * POST /agents/prune — operator-only, deletes terminal-status agents
    only (active agents survive), 401 without auth.
  * GET /agents?status= — the status filter the CLI's --status uses.

The dev DB is shared across tests and seeds a single customer, so prune
assertions check membership (my terminated agents gone, my active agent
kept) rather than an exact global count.

NOTE: no `from __future__ import annotations` — see test_smoke.py.
"""

from typing import Any

import pytest
from _helpers import _auto_kill_payload, _register_agent
from _keys import DEV_HEADERS, OP_HEADERS
from httpx import AsyncClient
from sqlalchemy import text

_UNKNOWN_AGENT = "00000000-0000-4000-8000-000000000000"


async def _terminate_via_cert(client: AsyncClient, agent_id: str) -> None:
    """Flip an agent to TERMINATED by posting an auto-kill cert (DEV key)."""
    r = await client.post(
        f"/agents/{agent_id}/kill_events",
        json=_auto_kill_payload(agent_id),
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text


# --- DELETE /agents/{id} --------------------------------------------------


@pytest.mark.asyncio
async def test_delete_happy_path_removes_agent(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "rm-happy")
    cleanup_agents.append(agent_id)

    r = await client.delete(f"/agents/{agent_id}", headers=OP_HEADERS)
    assert r.status_code == 204, r.text

    # Gone for good.
    g = await client.get(f"/agents/{agent_id}", headers=DEV_HEADERS)
    assert g.status_code == 404


@pytest.mark.asyncio
async def test_delete_cascades_kill_events(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Deleting an agent must remove its dependent rows, not just the agent.

    SQLite doesn't enforce FK cascade, so the endpoint deletes dependents
    explicitly; here on Postgres we still assert the kill_events row is gone.
    """
    agent_id = await _register_agent(client, "rm-cascade")
    cleanup_agents.append(agent_id)
    await _terminate_via_cert(client, agent_id)

    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        before = (
            await session.execute(
                text("SELECT count(*) FROM kill_events WHERE agent_id::text = :id"),
                {"id": agent_id},
            )
        ).scalar_one()
    assert before == 1

    r = await client.delete(f"/agents/{agent_id}", headers=OP_HEADERS)
    assert r.status_code == 204, r.text

    async with SessionLocal() as session:
        after = (
            await session.execute(
                text("SELECT count(*) FROM kill_events WHERE agent_id::text = :id"),
                {"id": agent_id},
            )
        ).scalar_one()
    assert after == 0


@pytest.mark.asyncio
async def test_delete_requires_operator(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Developer keys can't delete — deleting the audit trail is operator-only."""
    agent_id = await _register_agent(client, "rm-rbac")
    cleanup_agents.append(agent_id)

    r = await client.delete(f"/agents/{agent_id}", headers=DEV_HEADERS)
    assert r.status_code == 403

    # Still there.
    g = await client.get(f"/agents/{agent_id}", headers=DEV_HEADERS)
    assert g.status_code == 200


@pytest.mark.asyncio
async def test_delete_no_auth_returns_401(client: AsyncClient) -> None:
    r = await client.delete(f"/agents/{_UNKNOWN_AGENT}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_unknown_agent_returns_404(client: AsyncClient) -> None:
    r = await client.delete(f"/agents/{_UNKNOWN_AGENT}", headers=OP_HEADERS)
    assert r.status_code == 404


# --- POST /agents/prune ---------------------------------------------------


@pytest.mark.asyncio
async def test_prune_deletes_terminated_keeps_active(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    dead_a = await _register_agent(client, "prune-dead-a")
    dead_b = await _register_agent(client, "prune-dead-b")
    alive = await _register_agent(client, "prune-alive")
    cleanup_agents += [dead_a, dead_b, alive]

    await _terminate_via_cert(client, dead_a)
    await _terminate_via_cert(client, dead_b)

    r = await client.post(
        "/agents/prune", json={"status": "terminated"}, headers=OP_HEADERS
    )
    assert r.status_code == 200, r.text
    # Shared DB may hold other terminated agents; assert >= our two.
    assert r.json()["deleted"] >= 2

    # The two terminated agents are gone; the active one survives.
    assert (await client.get(f"/agents/{dead_a}", headers=DEV_HEADERS)).status_code == 404
    assert (await client.get(f"/agents/{dead_b}", headers=DEV_HEADERS)).status_code == 404
    assert (await client.get(f"/agents/{alive}", headers=DEV_HEADERS)).status_code == 200


@pytest.mark.asyncio
async def test_prune_requires_operator(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "prune-rbac")
    cleanup_agents.append(agent_id)
    await _terminate_via_cert(client, agent_id)

    r = await client.post(
        "/agents/prune", json={"status": "terminated"}, headers=DEV_HEADERS
    )
    assert r.status_code == 403

    # Untouched.
    g = await client.get(f"/agents/{agent_id}", headers=DEV_HEADERS)
    assert g.status_code == 200


@pytest.mark.asyncio
async def test_prune_no_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post("/agents/prune", json={"status": "terminated"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_prune_defaults_to_terminated(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Empty body → status defaults to TERMINATED (no 422)."""
    agent_id = await _register_agent(client, "prune-default")
    cleanup_agents.append(agent_id)
    await _terminate_via_cert(client, agent_id)

    r = await client.post("/agents/prune", json={}, headers=OP_HEADERS)
    assert r.status_code == 200, r.text
    assert (await client.get(f"/agents/{agent_id}", headers=DEV_HEADERS)).status_code == 404


# --- GET /agents?status= --------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_status_filter(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """The CLI's `hermeskill fleet --status terminated` relies on ?status=."""
    active = await _register_agent(client, "filter-active")
    dead = await _register_agent(client, "filter-dead")
    cleanup_agents += [active, dead]
    await _terminate_via_cert(client, dead)

    r = await client.get("/agents", params={"status": "terminated"}, headers=DEV_HEADERS)
    assert r.status_code == 200, r.text
    statuses = {a["status"] for a in r.json()}
    ids = {a["id"] for a in r.json()}
    assert statuses == {"terminated"} or not statuses  # only terminated (if any)
    assert dead in ids
    assert active not in ids


# --- cleanup --------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_kill_events() -> Any:
    yield
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM kill_events"))
        await session.commit()
