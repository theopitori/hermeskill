"""M2.6 — DoD steps 1-4 end-to-end against live Postgres.

For each `--induce` mode the demo agent ships with (loop / cost /
wall_clock / scope), this test:

  1. Builds the misbehaving graph through `demo.coding_agent.agent.build_graph`
  2. Runs it under `await watch(...)` against an in-process FastAPI app
     (`httpx.ASGITransport`) backed by the live dev Postgres
  3. Asserts the agent self-terminated cooperatively (`StasisTerminated`)
  4. Queries `/agents/{id}/kill_events` and asserts:
       * Exactly one kill_event was filed
       * status = confirmed
       * trigger_type = auto
       * trigger_reason is non-empty
       * the death certificate is present and non-empty
       * the symptoms_log contains the expected symptom
       * the agent row's status is now `terminated`

This is the test that proves M2 (the whole apoptosis path) works end-to-end.
If this test passes, you can demo the 9-step DoD steps 1-4 by hand.

NOTE: no `from __future__ import annotations` — see test_smoke.py for why.
"""

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from control_plane.main import app
from sqlalchemy import text
from stasis_agent import StasisTerminated, watch
from stasis_agent.client import StasisClient
from stasis_agent.watcher import (
    BackgroundWorker,
    _reset_registry_for_tests,
)

# Make the top-level `demo/` package importable.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from conftest import DEV_DEVELOPER_KEY  # noqa: E402  (sys.path mutated above)


@pytest.fixture(autouse=True)
def _isolate_sdk_state() -> Any:
    """Each test starts with a clean SDK registry + no worker singleton."""
    _reset_registry_for_tests()
    BackgroundWorker._instance = None
    yield
    _reset_registry_for_tests()
    BackgroundWorker._instance = None


@pytest.fixture(autouse=True)
async def _clean_kill_events() -> Any:
    """Drop kill_events rows between tests so the partial unique index
    doesn't accumulate state across runs against the same dev DB."""
    yield
    from control_plane.db.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM kill_events"))
        await session.commit()


# Each parametrize tuple: (induce_mode, expected_symptom_in_log).
# `expected_symptom_in_log` is the SymptomType.value the symptoms_log
# must contain at least one entry for.
@pytest.mark.parametrize(
    ("induce_mode", "expected_symptom"),
    [
        ("loop", "loop"),
        ("cost", "token_runaway"),
        ("wall_clock", "wall_clock"),
        ("scope", "tool_scope_violation"),
    ],
)
@pytest.mark.asyncio
async def test_dod_induce_kills_and_writes_full_death_certificate(
    induce_mode: str,
    expected_symptom: str,
    cleanup_agents: list[str],
) -> None:
    """The headline M2 contract: an induced kill leaves a complete death
    certificate on the control plane, retrievable via `GET /kill_events/{id}`."""
    from demo.coding_agent.agent import build_graph

    transport = httpx.ASGITransport(app=app)
    client = StasisClient(
        base_url="http://test",
        api_key=DEV_DEVELOPER_KEY,
        transport=transport,
    )

    try:
        # --- 1. watch() + invoke with induce mode ---
        graph = build_graph(induce=induce_mode)
        watched = await watch(
            graph,
            name=f"dod-e2e-{induce_mode}",
            policy="coding-default",
            metadata={"dod": True, "induce": induce_mode},
            client=client,
        )

        # The agent should self-terminate cooperatively.
        with pytest.raises(StasisTerminated):
            await watched.ainvoke({"task": "trip a symptom"})

        # Find the agent we just registered.
        agents = await client.list_agents()
        ours = [a for a in agents if a.name == f"dod-e2e-{induce_mode}"]
        assert len(ours) == 1, f"agent registration not visible: {ours!r}"
        agent_id = ours[0].id
        cleanup_agents.append(str(agent_id))

        # --- 2. agent row should now be TERMINATED ---
        # M2.5's cert POST flips the agent row to terminated synchronously.
        agent_detail = await client.get_agent(agent_id)
        assert agent_detail.status.value == "terminated", (
            f"agent status after kill: {agent_detail.status} (expected terminated)"
        )
        assert agent_detail.terminated_at is not None

        # --- 3. kill_event landed with full forensic payload ---
        kill_events = await client.list_kill_events(agent_id)
        assert len(kill_events) == 1, (
            f"expected exactly one kill_event for {induce_mode}, got {len(kill_events)}"
        )
        ke = kill_events[0]
        assert ke.status.value == "confirmed"
        assert ke.trigger_type.value == "auto"
        assert ke.trigger_reason  # non-empty
        assert ke.death_certificate is not None
        assert ke.terminated_at is not None

        cert = ke.death_certificate
        assert str(cert.agent_id) == str(agent_id)
        assert cert.trigger_type.value == "auto"
        # symptoms_log carries the expected symptom for this induce mode
        symptoms_in_log = {s["symptom"] for s in cert.symptoms_log}
        assert expected_symptom in symptoms_in_log, (
            f"symptoms_log for induce={induce_mode!r}: {symptoms_in_log!r}; "
            f"expected {expected_symptom!r}"
        )
        # shutdown_log must include the `apoptosis_requested` step that
        # `request_termination` records at the moment of decision. Steps
        # recorded AFTER the cert is built (death_cert_posted, etc.) won't
        # appear here — the cert is a snapshot at build time.
        cert_steps = {s.step for s in cert.shutdown_log}
        ke_steps = {s.step for s in ke.shutdown_log}
        all_steps = cert_steps | ke_steps
        assert "apoptosis_requested" in all_steps, (
            f"shutdown_log missing apoptosis_requested for induce={induce_mode!r}: "
            f"cert={list(cert_steps)} ke={list(ke_steps)}"
        )

        # --- 4. round-trip via GET /kill_events/{id} ---
        full = await client.get_kill_event(ke.id)
        assert full.id == ke.id
        assert full.death_certificate is not None
        assert full.trigger_reason == ke.trigger_reason
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_healthy_run_does_not_create_kill_event(
    cleanup_agents: list[str],
) -> None:
    """Negative regression: when no `--induce` is set, the demo completes
    normally and NO kill_event is created. Proves the wrapper's cert
    post is keyed on StasisTerminated, not on shutdown."""
    from demo.coding_agent.agent import build_graph

    transport = httpx.ASGITransport(app=app)
    client = StasisClient(
        base_url="http://test",
        api_key=DEV_DEVELOPER_KEY,
        transport=transport,
    )
    try:
        graph = build_graph()  # no induce
        watched = await watch(
            graph,
            name="dod-e2e-healthy",
            policy="coding-default",
            client=client,
        )
        result = await watched.ainvoke({"task": "fix the bug"})
        assert result["edits_made"] == 1

        agents = await client.list_agents()
        ours = [a for a in agents if a.name == "dod-e2e-healthy"]
        agent_id = ours[0].id
        cleanup_agents.append(str(agent_id))

        # No kill_event filed.
        kill_events = await client.list_kill_events(agent_id)
        assert kill_events == [], (
            f"healthy run should not file a kill_event, got: {kill_events!r}"
        )
        # And agent status is NOT terminated.
        detail = await client.get_agent(agent_id)
        assert detail.status.value != "terminated"
    finally:
        await BackgroundWorker.stop()
        await client.aclose()
