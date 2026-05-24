"""Tests for the M5 grants endpoints.

Covers:
  * POST /agents/{id}/grants — operator-only; policy + universal validation.
  * POST /grants/{id}/revoke — operator-only, idempotent.
  * GET /agents/{id}/grants — read; active_only filter.
  * Heartbeat enrichment — active grants in response.

NOTE: no `from __future__ import annotations` — see test_smoke.py.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _keys import DEV_HEADERS, OP_HEADERS
from httpx import AsyncClient
from sqlalchemy import text


async def _register_agent(
    client: AsyncClient, name: str, policy: str = "coding-default"
) -> str:
    r = await client.post(
        "/agents",
        json={"name": name, "policy_name": policy},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["agent_id"])


# --- POST /agents/{id}/grants --------------------------------------------


@pytest.mark.asyncio
async def test_grant_happy_path_against_coding_default(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """coding-default allows `tool_scope_violation` — issue it."""
    agent_id = await _register_agent(client, "grant-happy")
    cleanup_agents.append(agent_id)

    r = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 3600,
            "reason": "trying a new tool under supervision",
        },
        headers=OP_HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent_id"] == agent_id
    assert body["symptoms"] == ["tool_scope_violation"]
    assert body["active"] is True
    assert body["revoked_at"] is None
    # issued_by stamps the operator's api_key_id.
    assert body["issued_by"] is not None


@pytest.mark.asyncio
async def test_grant_requires_operator(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "grant-rbac")
    cleanup_agents.append(agent_id)
    r = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 60,
            "reason": "x",
        },
        headers=DEV_HEADERS,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_grant_rejects_manual_kill(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """manual_kill is never grantable — universal rule, not policy-derived."""
    agent_id = await _register_agent(client, "grant-no-manual")
    cleanup_agents.append(agent_id)
    r = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["manual_kill"],
            "duration_seconds": 60,
            "reason": "should be rejected",
        },
        headers=OP_HEADERS,
    )
    assert r.status_code == 422
    assert "manual_kill" in r.json()["detail"]


@pytest.mark.asyncio
async def test_grant_rejects_disallowed_symptom_for_policy(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """coding-default does NOT allow grants for `token_runaway` — operators
    shouldn't be able to grant unlimited spend."""
    agent_id = await _register_agent(client, "grant-disallowed-symptom")
    cleanup_agents.append(agent_id)
    r = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["token_runaway"],
            "duration_seconds": 60,
            "reason": "please let me overspend",
        },
        headers=OP_HEADERS,
    )
    assert r.status_code == 422
    assert "token_runaway" in r.json()["detail"]


@pytest.mark.asyncio
async def test_grant_rejects_for_strict_policy(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """strict policy has `allowed_symptoms=[]` — nothing is grantable."""
    agent_id = await _register_agent(
        client, "grant-strict", policy="strict"
    )
    cleanup_agents.append(agent_id)
    r = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["loop"],
            "duration_seconds": 60,
            "reason": "x",
        },
        headers=OP_HEADERS,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_grant_rejects_over_24h(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "grant-over-cap")
    cleanup_agents.append(agent_id)
    r = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            # 25h — over the hard cap of 24h.
            "duration_seconds": 25 * 3600,
            "reason": "x",
        },
        headers=OP_HEADERS,
    )
    # Pydantic catches this on the wire as 422.
    assert r.status_code == 422


# --- POST /grants/{id}/revoke --------------------------------------------


@pytest.mark.asyncio
async def test_revoke_happy_path(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "revoke-happy")
    cleanup_agents.append(agent_id)
    r1 = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "to be revoked",
        },
        headers=OP_HEADERS,
    )
    grant_id = r1.json()["id"]

    r2 = await client.post(
        f"/grants/{grant_id}/revoke",
        json={"reason": "ok enough exploration"},
        headers=OP_HEADERS,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["revoked_at"] is not None
    assert body["revoked_by"] is not None
    assert body["revoke_reason"] == "ok enough exploration"
    assert body["active"] is False


@pytest.mark.asyncio
async def test_revoke_is_idempotent(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """A second revoke returns 200 with the unchanged row, not 409."""
    agent_id = await _register_agent(client, "revoke-idempotent")
    cleanup_agents.append(agent_id)
    r1 = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "x",
        },
        headers=OP_HEADERS,
    )
    grant_id = r1.json()["id"]

    r2 = await client.post(
        f"/grants/{grant_id}/revoke",
        json={"reason": "first revoke"},
        headers=OP_HEADERS,
    )
    first_at = r2.json()["revoked_at"]
    assert first_at is not None

    r3 = await client.post(
        f"/grants/{grant_id}/revoke",
        json={"reason": "second revoke"},
        headers=OP_HEADERS,
    )
    assert r3.status_code == 200
    # Unchanged: same revoked_at, same revoke_reason.
    assert r3.json()["revoked_at"] == first_at
    assert r3.json()["revoke_reason"] == "first revoke"


@pytest.mark.asyncio
async def test_revoke_unknown_grant_returns_404(client: AsyncClient) -> None:
    r = await client.post(
        "/grants/00000000-0000-4000-8000-000000000000/revoke",
        json={"reason": "x"},
        headers=OP_HEADERS,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_revoke_requires_operator(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "revoke-rbac")
    cleanup_agents.append(agent_id)
    r1 = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 60,
            "reason": "x",
        },
        headers=OP_HEADERS,
    )
    grant_id = r1.json()["id"]
    r2 = await client.post(
        f"/grants/{grant_id}/revoke",
        json={"reason": "x"},
        headers=DEV_HEADERS,
    )
    assert r2.status_code == 403


# --- GET /agents/{id}/grants ---------------------------------------------


@pytest.mark.asyncio
async def test_list_grants_returns_all_then_active_only(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "list-grants")
    cleanup_agents.append(agent_id)

    # Issue two grants, revoke one.
    r1 = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "to be revoked",
        },
        headers=OP_HEADERS,
    )
    revoked_id = r1.json()["id"]
    await client.post(
        f"/grants/{revoked_id}/revoke",
        json={"reason": "x"},
        headers=OP_HEADERS,
    )
    await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "still active",
        },
        headers=OP_HEADERS,
    )

    all_resp = await client.get(
        f"/agents/{agent_id}/grants", headers=DEV_HEADERS
    )
    assert all_resp.status_code == 200
    assert len(all_resp.json()) == 2

    active_resp = await client.get(
        f"/agents/{agent_id}/grants?active_only=true", headers=DEV_HEADERS
    )
    assert active_resp.status_code == 200
    actives = active_resp.json()
    assert len(actives) == 1
    assert actives[0]["active"] is True


# --- heartbeat enrichment ------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_returns_active_grants(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "heartbeat-grants")
    cleanup_agents.append(agent_id)
    await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "explore",
        },
        headers=OP_HEADERS,
    )

    r = await client.post(
        f"/agents/{agent_id}/heartbeat",
        json={"uptime_seconds": 1.0},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 200
    grants = r.json()["active_grants"]
    assert len(grants) == 1
    g = grants[0]
    assert "tool_scope_violation" in g["symptoms"]
    assert g["reason"] == "explore"


@pytest.mark.asyncio
async def test_heartbeat_excludes_revoked_and_expired_grants(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "heartbeat-filter")
    cleanup_agents.append(agent_id)

    # Issue one + revoke; issue another + backdate expiry.
    r1 = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "to revoke",
        },
        headers=OP_HEADERS,
    )
    revoked_id = r1.json()["id"]
    await client.post(
        f"/grants/{revoked_id}/revoke",
        json={"reason": "x"},
        headers=OP_HEADERS,
    )

    r2 = await client.post(
        f"/agents/{agent_id}/grants",
        json={
            "symptoms": ["tool_scope_violation"],
            "duration_seconds": 600,
            "reason": "will be expired by hand",
        },
        headers=OP_HEADERS,
    )
    expired_id = r2.json()["id"]
    # Backdate.
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE apoptosis_grants SET expires_at = :past "
                "WHERE id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(minutes=1), "id": expired_id},
        )
        await session.commit()

    hb = await client.post(
        f"/agents/{agent_id}/heartbeat",
        json={"uptime_seconds": 1.0},
        headers=DEV_HEADERS,
    )
    assert hb.json()["active_grants"] == []


# --- cleanup -------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_grants() -> Any:
    yield
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM apoptosis_grants"))
        await session.commit()


def test_router_import_smoke() -> None:
    from control_plane.api.grants import router, top_router

    paths = {r.path for r in router.routes}
    assert "/agents/{agent_id}/grants" in paths
    top_paths = {r.path for r in top_router.routes}
    assert "/grants/{grant_id}/revoke" in top_paths
