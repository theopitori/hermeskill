"""Manual-kill router — operator-issued cooperative termination (M4).

`POST /agents/{id}/terminate` lets an operator trigger a cooperative kill
without waiting for the agent to self-detect a Terminal symptom. The SDK's
kill-pending poller discovers the INITIATED row, starts cooperative shutdown,
and posts the death certificate via the M2.5 cert endpoint — which promotes
INITIATED → CONFIRMED and flips `agent.status = TERMINATED`.

`GET /kills/pending` is the batch endpoint the SDK's kill-pending poller
calls every `kill_poll_interval_seconds`. It returns all INITIATED manual
kills for the caller's agents in one round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from caspase.types import KillEventOut, PendingKillOut, TerminateAgentIn
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api._kill_event_serializers import _kill_event_out
from control_plane.api.agents import _load_agent_owned_by
from control_plane.auth import Principal, require_operator, require_principal
from control_plane.db.models import (
    Agent,
    AgentStatus,
    KillEvent,
    KillEventStatus,
    TriggerType,
)
from control_plane.db.session import get_session

router = APIRouter(prefix="/agents", tags=["kill_events"])
kills_router = APIRouter(prefix="/kills", tags=["kill_events"])


@router.post(
    "/{agent_id}/terminate",
    status_code=status.HTTP_201_CREATED,
    response_model=KillEventOut,
    responses={
        409: {"description": "Agent already has an active kill_event"},
        403: {"description": "operator role required"},
    },
)
async def terminate_agent(
    agent_id: UUID,
    payload: TerminateAgentIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator)],
) -> KillEventOut:
    """Operator-issued manual kill.

    Inserts a `kill_events` row with `status=INITIATED`,
    `trigger_type=MANUAL`, `operator_id=principal.api_key_id`. The SDK's
    kill-pending poller sees the row, asks the watcher to start
    cooperative shutdown, and posts the death certificate via the M2.5
    path — that UPDATE promotes INITIATED → CONFIRMED and flips
    `agent.status = TERMINATED`.

    The partial unique index `ux_kill_events_one_active_per_agent` is
    the idempotency guard: a second `/terminate` while one is in flight
    fails the index → 409 with the existing id in the body.

    No feedback token is minted here — that happens on the cert POST.
    A manual kill whose SDK never cooperates won't get a feedback URL;
    that's acceptable (the zombie sweeper, deferred, is the remediation
    path for that worst case).
    """
    now = datetime.now(UTC)
    agent = await _load_agent_owned_by(session, agent_id, principal.customer_id)
    if agent.status == AgentStatus.TERMINATED:
        # Cheap pre-check — index would catch this too, but the message
        # is clearer when it's about a finished agent. Return the most-
        # recent kill_event id so the CLI can link the operator to the
        # existing cert instead of printing a sentinel.
        recent_stmt = (
            select(KillEvent.id)
            .where(KillEvent.agent_id == agent_id)
            .order_by(KillEvent.id.desc())
            .limit(1)
        )
        recent_id = (await session.execute(recent_stmt)).scalar_one_or_none()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "agent already terminated",
                "existing_kill_event_id": recent_id,
            },
        )

    kill_event = KillEvent(
        agent_id=agent_id,
        trigger_type=TriggerType.MANUAL,
        trigger_reason="manual kill",
        triggered_at=now,
        status=KillEventStatus.INITIATED,
        operator_id=principal.api_key_id,
        operator_reason=payload.reason,
    )
    session.add(kill_event)
    # Mark the agent as DYING. This is asymmetric with auto-kills, which
    # go straight RUNNING → TERMINATED via the M2.5 cert POST. The
    # asymmetry is intentional: manual kills have a measurable cooperative
    # window (poll → grace → cert) where DYING signals "we're doing it".
    agent.status = AgentStatus.DYING

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        existing_stmt = select(KillEvent).where(
            KillEvent.agent_id == agent_id,
            KillEvent.status.in_(
                [KillEventStatus.INITIATED, KillEventStatus.CONFIRMED]
            ),
        )
        winner = (await session.execute(existing_stmt)).scalar_one_or_none()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "agent already has an active kill_event",
                "existing_kill_event_id": winner.id if winner else None,
            },
        ) from exc

    await session.refresh(kill_event)
    return _kill_event_out(kill_event)


@kills_router.get("/pending", response_model=list[PendingKillOut])
async def list_pending_kills(
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> list[PendingKillOut]:
    """Batch endpoint the SDK's kill-pending poller calls every
    `kill_poll_interval_seconds`.

    Returns every `kill_events` row that:
      * belongs to one of the caller's agents (customer scope),
      * has `status=INITIATED`,
      * has `trigger_type=MANUAL`.

    Auto-kills aren't surfaced here — the SDK originates them, so it
    already knows. Confirmed kills aren't either — the cert has been
    posted, the agent has already cooperated. This is purely the
    "operator pulled the lever; agent doesn't know yet" set.

    One round-trip serves all watched agents in this process (TODO #8).
    """
    stmt = (
        select(KillEvent)
        .join(Agent, Agent.id == KillEvent.agent_id)
        .where(
            Agent.customer_id == principal.customer_id,
            KillEvent.status == KillEventStatus.INITIATED,
            KillEvent.trigger_type == TriggerType.MANUAL,
        )
        .order_by(KillEvent.id.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        PendingKillOut(
            agent_id=row.agent_id,
            kill_event_id=row.id,
            trigger_reason=row.trigger_reason,
            triggered_at=row.triggered_at,
            operator_reason=row.operator_reason,
            operator=str(row.operator_id) if row.operator_id else None,
        )
        for row in rows
    ]
