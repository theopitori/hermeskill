"""Tests for the M4 manual-kill endpoints.

Covers:
  * POST /agents/{id}/terminate — operator-only, 409 race, agent flipped
    to DYING, audit fields stamped.
  * GET /kills/pending — only INITIATED + MANUAL rows surface; auto
    and confirmed kills don't.

NOTE: no `from __future__ import annotations` — see test_smoke.py.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from _keys import DEV_HEADERS, OP_HEADERS
from httpx import AsyncClient
from sqlalchemy import text


async def _register_agent(client: AsyncClient, name: str) -> str:
    r = await client.post(
        "/agents",
        json={"name": name, "policy_name": "coding-default"},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["agent_id"])


def _auto_kill_payload(agent_id: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "trigger_type": "auto",
        "trigger_reason": "loop_detected",
        "triggered_at": now,
        "terminated_at": now,
        "death_certificate": {
            "agent_id": agent_id,
            "triggered_at": now,
            "terminated_at": now,
            "trigger_type": "auto",
            "trigger_reason": "loop_detected",
            "symptoms_log": [],
            "final_state": {},
            "shutdown_log": [],
            "operator": None,
            "operator_reason": None,
        },
        "shutdown_log": [],
    }


# --- POST /terminate -----------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_happy_path_writes_initiated_row(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "terminate-happy")
    cleanup_agents.append(agent_id)

    r = await client.post(
        f"/agents/{agent_id}/terminate",
        json={"reason": "deploy rollback"},
        headers=OP_HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["trigger_type"] == "manual"
    assert body["status"] == "initiated"
    assert body["operator_reason"] == "deploy rollback"

    # Agent flipped to DYING (asymmetric with auto-kill — see endpoint
    # docstring).
    a = await client.get(f"/agents/{agent_id}", headers=DEV_HEADERS)
    assert a.json()["status"] == "dying"


@pytest.mark.asyncio
async def test_terminate_requires_operator(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Developer keys can't issue kills — keeps the audit trail clean."""
    agent_id = await _register_agent(client, "terminate-rbac")
    cleanup_agents.append(agent_id)

    r = await client.post(
        f"/agents/{agent_id}/terminate",
        json={"reason": "should be rejected"},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_terminate_no_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post(
        "/agents/00000000-0000-4000-8000-000000000000/terminate",
        json={"reason": "anon"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_terminate_409_when_already_initiated(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Partial unique index → 409 on second /terminate."""
    agent_id = await _register_agent(client, "terminate-409")
    cleanup_agents.append(agent_id)

    r1 = await client.post(
        f"/agents/{agent_id}/terminate",
        json={"reason": "first"},
        headers=OP_HEADERS,
    )
    assert r1.status_code == 201
    first_id = r1.json()["id"]

    r2 = await client.post(
        f"/agents/{agent_id}/terminate",
        json={"reason": "should lose"},
        headers=OP_HEADERS,
    )
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["existing_kill_event_id"] == first_id


@pytest.mark.asyncio
async def test_terminate_409_when_auto_kill_already_landed(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """If the SDK already posted an auto-kill cert, manual /terminate
    must 409 — the agent is finished. The body must point the operator
    at the existing cert so the CLI doesn't fall back to a sentinel id."""
    agent_id = await _register_agent(client, "terminate-after-auto")
    cleanup_agents.append(agent_id)

    r1 = await client.post(
        f"/agents/{agent_id}/kill_events",
        json=_auto_kill_payload(agent_id),
        headers=DEV_HEADERS,
    )
    assert r1.status_code == 201
    auto_kill_id = r1.json()["id"]

    r2 = await client.post(
        f"/agents/{agent_id}/terminate",
        json={"reason": "too late"},
        headers=OP_HEADERS,
    )
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["existing_kill_event_id"] == auto_kill_id


# --- GET /kills/pending --------------------------------------------------


@pytest.mark.asyncio
async def test_pending_kills_returns_manual_initiated_only(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Auto kills + confirmed kills + other customers' kills must NOT
    appear in the pending list."""
    manual_agent = await _register_agent(client, "pending-manual")
    cleanup_agents.append(manual_agent)
    auto_agent = await _register_agent(client, "pending-auto")
    cleanup_agents.append(auto_agent)

    # Manual: issue but don't confirm.
    rm = await client.post(
        f"/agents/{manual_agent}/terminate",
        json={"reason": "queue this"},
        headers=OP_HEADERS,
    )
    assert rm.status_code == 201
    manual_kill_id = rm.json()["id"]

    # Auto: full cert post, status=confirmed.
    ra = await client.post(
        f"/agents/{auto_agent}/kill_events",
        json=_auto_kill_payload(auto_agent),
        headers=DEV_HEADERS,
    )
    assert ra.status_code == 201

    rp = await client.get("/kills/pending", headers=DEV_HEADERS)
    assert rp.status_code == 200
    pending = rp.json()
    ids = [p["kill_event_id"] for p in pending]
    assert manual_kill_id in ids
    # Auto agent must not appear — its kill is already confirmed AND
    # auto-typed (two reasons it should be excluded).
    auto_agent_ids = [
        p["agent_id"] for p in pending if p["agent_id"] == auto_agent
    ]
    assert auto_agent_ids == []


@pytest.mark.asyncio
async def test_pending_kills_no_auth_returns_401(client: AsyncClient) -> None:
    r = await client.get("/kills/pending")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pending_kills_payload_shape(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "pending-shape")
    cleanup_agents.append(agent_id)
    r = await client.post(
        f"/agents/{agent_id}/terminate",
        json={"reason": "explicit reason"},
        headers=OP_HEADERS,
    )
    assert r.status_code == 201

    rp = await client.get("/kills/pending", headers=DEV_HEADERS)
    matching = [p for p in rp.json() if p["agent_id"] == agent_id]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["operator_reason"] == "explicit reason"
    assert entry["operator"] is not None  # the operator api_key_id
    assert entry["trigger_reason"] == "manual kill"


# --- cleanup -------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_kill_events() -> Any:
    yield
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM kill_events"))
        await session.commit()


# --- type smoke ----------------------------------------------------------


def test_router_imports_smoke() -> None:
    from control_plane.api.kill_events import kills_router, router

    # Each router exposes the expected routes.
    paths = {r.path for r in router.routes}
    assert "/agents/{agent_id}/terminate" in paths
    paths_k = {r.path for r in kills_router.routes}
    assert "/kills/pending" in paths_k
