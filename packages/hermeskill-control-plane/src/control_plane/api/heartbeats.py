"""Heartbeats router.

The SDK's `HeartbeatBatcher` (TODO #8) sends one heartbeat per registered
agent per interval. We update `agents.last_heartbeat_at`, record a single
event row for audit, and (M5) attach the agent's active grants to the
response so the SDK can refresh its grant cache.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from hermeskill.types import EventType, HeartbeatIn, HeartbeatOut
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.agents import _load_agent_owned_by
from control_plane.api.grants import load_active_grants
from control_plane.auth import Principal, require_principal
from control_plane.db.models import Event
from control_plane.db.session import get_session

router = APIRouter(prefix="/agents", tags=["heartbeats"])


@router.post(
    "/{agent_id}/heartbeat",
    status_code=status.HTTP_200_OK,
    response_model=HeartbeatOut,
)
async def post_heartbeat(
    agent_id: UUID,
    payload: HeartbeatIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> HeartbeatOut:
    agent = await _load_agent_owned_by(session, agent_id, principal.customer_id)
    now = datetime.now(UTC)
    agent.last_heartbeat_at = now
    # Record one event so `hermeskill logs` can show heartbeat cadence.
    session.add(
        Event(
            agent_id=agent.id,
            type=EventType.HEARTBEAT.value,
            payload={"uptime_seconds": payload.uptime_seconds},
        )
    )
    await session.commit()
    grants = await load_active_grants(session, agent_id)
    return HeartbeatOut(received_at=now, active_grants=grants)
