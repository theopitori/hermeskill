"""Integration tests for the M3 feedback endpoint.

End-to-end exercises the symmetric-hash invariant (TODO.md #9): the
SDK-facing POST /agents/{id}/kill_events mints a token and stores its
hash; the public POST /feedback/{token} hashes the URL's raw token
before lookup. The round-trip test asserts the kill_events row gets
feedback_label + feedback_at populated.

NOTE: no `from __future__ import annotations` — see test_smoke.py.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import DEV_HEADERS
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


def _sample_payload(agent_id: str) -> dict[str, Any]:
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


async def _post_kill_event(client: AsyncClient, agent_id: str) -> dict[str, Any]:
    r = await client.post(
        f"/agents/{agent_id}/kill_events",
        json=_sample_payload(agent_id),
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _extract_raw_token(feedback_url: str) -> str:
    """The URL shape is `{base}/feedback/{raw}`; everything after the last
    `/feedback/` is the raw token."""
    marker = "/feedback/"
    assert marker in feedback_url, feedback_url
    return feedback_url.rsplit(marker, 1)[1]


# --- happy path: round-trip a feedback token end-to-end -----------------


@pytest.mark.asyncio
async def test_kill_event_response_includes_feedback_url(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "feedback-url-present")
    cleanup_agents.append(agent_id)
    body = await _post_kill_event(client, agent_id)

    cert = body["death_certificate"]
    assert cert is not None
    assert cert["feedback_url"], "death cert must carry a feedback URL"
    assert "/feedback/" in cert["feedback_url"]


@pytest.mark.asyncio
async def test_feedback_round_trip_updates_kill_event(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Mint a cert, submit feedback, verify the kill_events row updated."""
    agent_id = await _register_agent(client, "feedback-round-trip")
    cleanup_agents.append(agent_id)
    body = await _post_kill_event(client, agent_id)
    kill_event_id = body["id"]
    raw_token = _extract_raw_token(body["death_certificate"]["feedback_url"])

    r = await client.post(
        f"/feedback/{raw_token}",
        json={"label": "good_kill"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kill_event_id"] == kill_event_id
    assert out["label"] == "good_kill"

    # Verify the persisted row reflects the feedback. Also pins the
    # contract that GET keeps surfacing feedback_url in the cert — guards
    # against an accidental serializer regression.
    r2 = await client.get(f"/kill_events/{kill_event_id}", headers=DEV_HEADERS)
    assert r2.status_code == 200
    assert (
        r2.json()["death_certificate"]["feedback_url"]
        == body["death_certificate"]["feedback_url"]
    )
    # KillEventOut doesn't surface feedback_label directly today, so go to
    # the DB and read it. This also confirms the column was written, not
    # just the API-level state.
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT feedback_label, feedback_at FROM kill_events "
                "WHERE id = :id"
            ),
            {"id": kill_event_id},
        )
        label, at = row.one()
        assert label == "good_kill"
        assert at is not None


@pytest.mark.asyncio
async def test_feedback_no_auth_required(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """The endpoint is intentionally public — token IS the auth."""
    agent_id = await _register_agent(client, "feedback-no-auth")
    cleanup_agents.append(agent_id)
    body = await _post_kill_event(client, agent_id)
    raw_token = _extract_raw_token(body["death_certificate"]["feedback_url"])

    # No Authorization header.
    r = await client.post(
        f"/feedback/{raw_token}",
        json={"label": "false_positive"},
    )
    assert r.status_code == 200, r.text


# --- 404 / 410 / expiry --------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_token_returns_404(client: AsyncClient) -> None:
    r = await client.post(
        "/feedback/not-a-real-token-just-some-string",
        json={"label": "good_kill"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reused_token_returns_410(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "feedback-reuse")
    cleanup_agents.append(agent_id)
    body = await _post_kill_event(client, agent_id)
    raw_token = _extract_raw_token(body["death_certificate"]["feedback_url"])

    r1 = await client.post(
        f"/feedback/{raw_token}", json={"label": "good_kill"}
    )
    assert r1.status_code == 200

    r2 = await client.post(
        f"/feedback/{raw_token}", json={"label": "missed_kill"}
    )
    assert r2.status_code == 410


@pytest.mark.asyncio
async def test_expired_token_returns_404(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    """Backdate the token's expiry; lookup should 404, not 200."""
    agent_id = await _register_agent(client, "feedback-expired")
    cleanup_agents.append(agent_id)
    body = await _post_kill_event(client, agent_id)
    raw_token = _extract_raw_token(body["death_certificate"]["feedback_url"])

    from control_plane.db.session import SessionLocal
    from control_plane.feedback_tokens import hash_feedback_token

    token_hash = hash_feedback_token(raw_token)
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE feedback_tokens SET expires_at = :past "
                "WHERE token_hash = :h"
            ),
            {"past": past, "h": token_hash},
        )
        await session.commit()

    r = await client.post(
        f"/feedback/{raw_token}", json={"label": "good_kill"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bad_label_returns_422(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    agent_id = await _register_agent(client, "feedback-bad-label")
    cleanup_agents.append(agent_id)
    body = await _post_kill_event(client, agent_id)
    raw_token = _extract_raw_token(body["death_certificate"]["feedback_url"])

    r = await client.post(
        f"/feedback/{raw_token}",
        json={"label": "thumbs_up"},  # not in FeedbackLabel
    )
    assert r.status_code == 422


# --- cleanup -------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_feedback_state() -> Any:
    """Wipe kill_events between tests (feedback_tokens cascade off this)."""
    yield
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM kill_events"))
        await session.commit()


# --- type smoke ----------------------------------------------------------


def test_router_import_smoke() -> None:
    from control_plane.api.feedback import router

    assert router is not None
    # Sanity: UUID import works (parallels test_kill_events.py).
    assert isinstance(UUID("00000000-0000-0000-0000-000000000000"), UUID)
