"""Shared async helpers for control-plane integration tests."""

from datetime import UTC, datetime
from typing import Any

from _keys import DEV_HEADERS
from httpx import AsyncClient


async def _register_agent(
    client: AsyncClient, name: str, *, policy: str = "coding-default"
) -> str:
    r = await client.post(
        "/agents",
        json={"name": name, "policy_name": policy},
        headers=DEV_HEADERS,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["agent_id"])


def _sample_payload(agent_id: str, reason: str = "loop_detected") -> dict[str, Any]:
    """Minimal-but-complete kill_event POST body with a loop symptom."""
    now = datetime.now(UTC)
    return {
        "trigger_type": "auto",
        "trigger_reason": reason,
        "triggered_at": now.isoformat(),
        "terminated_at": now.isoformat(),
        "death_certificate": {
            "agent_id": agent_id,
            "triggered_at": now.isoformat(),
            "terminated_at": now.isoformat(),
            "trigger_type": "auto",
            "trigger_reason": reason,
            "symptoms_log": [
                {
                    "symptom": "loop",
                    "severity": "terminal",
                    "reason": reason,
                    "detail": {"count": 5},
                    "at": now.isoformat(),
                }
            ],
            "final_state": {},
            "shutdown_log": [],
            "operator": None,
            "operator_reason": None,
        },
        "shutdown_log": [
            {
                "step": "apoptosis_requested",
                "at": now.isoformat(),
                "duration_ms": 0.5,
                "detail": {},
            }
        ],
    }


def _auto_kill_payload(agent_id: str) -> dict[str, Any]:
    """Minimal auto-kill payload without a symptoms_log — for manual-kill tests."""
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
