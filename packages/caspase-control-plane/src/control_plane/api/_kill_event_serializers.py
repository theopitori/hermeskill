"""Internal serializer for KillEvent ORM → API response."""

from __future__ import annotations

from caspase.types import KillEventOut

from control_plane.db.models import KillEvent


def _kill_event_out(row: KillEvent) -> KillEventOut:
    """Convert a SQLAlchemy KillEvent into the API response model."""
    return KillEventOut.model_validate(
        {
            "id": row.id,
            "agent_id": row.agent_id,
            "trigger_type": row.trigger_type,
            "trigger_reason": row.trigger_reason,
            "status": row.status,
            "triggered_at": row.triggered_at,
            "terminated_at": row.terminated_at,
            "death_certificate": row.death_certificate,
            "shutdown_log": row.shutdown_log or [],
            "operator_reason": row.operator_reason,
            "created_at": row.created_at,
        }
    )
