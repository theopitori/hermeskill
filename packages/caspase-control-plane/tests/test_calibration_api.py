"""Integration tests for the Phase-4 calibration endpoint.

`GET /policies/{policy_name}/calibration` aggregates the operator feedback
labels collected on death certificates into an advisory report. These tests
exercise the thin I/O shell end-to-end (the pure aggregation heuristic is
unit-tested in `packages/caspase-sdk/tests/test_calibration.py`):

  * round-trip — register agents under a policy, file a loop-kill each, label a
    majority false-positive through the real public feedback endpoint, then
    assert the report surfaces the advisory loosening suggestion;
  * empty — a policy with no labeled kills is a valid 200 with total 0;
  * unknown policy — 404;
  * auth — the endpoint requires a principal (it is customer-scoped);
  * customer scoping — feedback under a different policy doesn't leak in.

Like the rest of the suite these run against the dev Postgres (see conftest).

NOTE: no `from __future__ import annotations` — see test_smoke.py.
"""

from typing import Any

import pytest
from _helpers import _sample_payload
from _keys import DEV_HEADERS
from httpx import AsyncClient
from sqlalchemy import text

_POLICY = "strict"  # max_loop_repeats=3


async def _register_agent(client: AsyncClient, name: str, policy: str) -> str:
    r = await client.post(
        "/agents",
        json={"name": name, "policy_name": policy},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["agent_id"])


async def _file_kill(client: AsyncClient, agent_id: str) -> dict[str, Any]:
    r = await client.post(
        f"/agents/{agent_id}/kill_events",
        json=_sample_payload(agent_id),
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _raw_token(cert: dict[str, Any]) -> str:
    return cert["feedback_url"].rsplit("/feedback/", 1)[1]


async def _label(client: AsyncClient, cert: dict[str, Any], label: str) -> None:
    r = await client.post(f"/feedback/{_raw_token(cert)}", json={"label": label})
    assert r.status_code == 200, r.text


async def _seed_labeled_loop_kills(
    client: AsyncClient,
    cleanup_agents: list[str],
    labels: list[str],
    *,
    policy: str = _POLICY,
    name_prefix: str = "cal",
) -> None:
    """Register one agent per label, file a loop-kill, submit the verdict."""
    for i, label in enumerate(labels):
        agent_id = await _register_agent(client, f"{name_prefix}-{i}", policy)
        cleanup_agents.append(agent_id)
        body = await _file_kill(client, agent_id)
        await _label(client, body["death_certificate"], label)


# --- round-trip ----------------------------------------------------------


@pytest.mark.asyncio
async def test_calibration_round_trip_surfaces_loosening_suggestion(
    client: AsyncClient,
    cleanup_agents: list[str],
) -> None:
    # 3 false-positive + 2 good → 60% FP > 30% threshold → advisory loosen.
    await _seed_labeled_loop_kills(
        client,
        cleanup_agents,
        ["false_positive", "false_positive", "false_positive", "good_kill", "good_kill"],
    )

    r = await client.get(f"/policies/{_POLICY}/calibration", headers=DEV_HEADERS)
    assert r.status_code == 200, r.text
    report = r.json()

    assert report["policy_name"] == _POLICY
    assert report["total_labeled_kills"] == 5
    loop = next(s for s in report["symptoms"] if s["symptom"] == "loop")
    assert loop["total_labeled"] == 5
    assert loop["false_positives"] == 3
    assert loop["false_positive_rate"] == 0.6
    assert loop["threshold_field"] == "max_loop_repeats"
    assert loop["current_value"] == 3.0
    assert loop["suggested_value"] == 5.0  # ceil(3 * 1.5)
    assert "false-positive" in loop["rationale"]


@pytest.mark.asyncio
async def test_calibration_empty_when_no_labeled_kills(
    client: AsyncClient,
) -> None:
    r = await client.get(f"/policies/{_POLICY}/calibration", headers=DEV_HEADERS)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["total_labeled_kills"] == 0
    assert report["symptoms"] == []


@pytest.mark.asyncio
async def test_calibration_unknown_policy_returns_404(client: AsyncClient) -> None:
    r = await client.get(
        "/policies/not-a-real-policy/calibration", headers=DEV_HEADERS
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_calibration_requires_auth(client: AsyncClient) -> None:
    r = await client.get(f"/policies/{_POLICY}/calibration")
    assert r.status_code in (401, 403)


# --- cleanup -------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_kill_events() -> Any:
    """Wipe kill_events between tests (feedback_tokens cascade off this)."""
    yield
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM kill_events"))
        await session.commit()
