"""Agents router: register, fleet, get one, delete, prune.

M4 adds POST /agents/{id}/terminate and GET /agents/{id}/kill-pending.
Fleet management adds DELETE /agents/{id} and POST /agents/prune so terminal
agents don't accumulate forever.
"""

from typing import Annotated
from uuid import UUID, uuid4

from hermeskill.types import (
    AgentRegistrationIn,
    AgentRegistrationOut,
    AgentStatus,
    AgentSummary,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth import Principal, require_operator, require_principal
from control_plane.db.models import (
    Agent,
    ApoptosisGrant,
    Event,
    FeedbackToken,
    KillEvent,
)
from control_plane.db.session import get_session

router = APIRouter(prefix="/agents", tags=["agents"])


class PruneIn(BaseModel):
    status: AgentStatus = AgentStatus.TERMINATED


class PruneOut(BaseModel):
    deleted: int


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AgentRegistrationOut)
async def register_agent(
    payload: AgentRegistrationIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> AgentRegistrationOut:
    agent_id = uuid4()
    agent = Agent(
        id=agent_id,
        customer_id=principal.customer_id,
        policy_name=payload.policy_name,
        name=payload.name,
        status=AgentStatus.REGISTERED,
        metadata_=payload.metadata,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return AgentRegistrationOut(
        agent_id=agent.id,
        policy_name=agent.policy_name,
        registered_at=agent.registered_at,
    )


@router.get("", response_model=list[AgentSummary])
async def list_agents(
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
    status_filter: Annotated[AgentStatus | None, Query(alias="status")] = None,
) -> list[AgentSummary]:
    stmt = (
        select(Agent)
        .where(Agent.customer_id == principal.customer_id)
        .order_by(Agent.registered_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Agent.status == status_filter)
    result = await session.execute(stmt)
    agents = result.scalars().all()
    return [
        AgentSummary(
            id=a.id,
            name=a.name,
            policy_name=a.policy_name,
            status=AgentStatus(a.status),
            registered_at=a.registered_at,
            last_heartbeat_at=a.last_heartbeat_at,
            terminated_at=a.terminated_at,
        )
        for a in agents
    ]


@router.get("/{agent_id}", response_model=AgentSummary)
async def get_agent(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> AgentSummary:
    agent = await _load_agent_owned_by(session, agent_id, principal.customer_id)
    return AgentSummary(
        id=agent.id,
        name=agent.name,
        policy_name=agent.policy_name,
        status=AgentStatus(agent.status),
        registered_at=agent.registered_at,
        last_heartbeat_at=agent.last_heartbeat_at,
        terminated_at=agent.terminated_at,
    )


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator)],
) -> Response:
    """Delete an agent and all its history. Operator-only, irreversible.

    Deleting the audit trail is more destructive than killing, so this is
    gated on the operator role (same as POST /terminate). 404s on a missing
    or cross-customer agent.
    """
    await _load_agent_owned_by(session, agent_id, principal.customer_id)
    await _cascade_delete_agents(session, [agent_id])
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/prune", response_model=PruneOut)
async def prune_agents(
    payload: PruneIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator)],
) -> PruneOut:
    """Bulk-delete the caller's agents in a terminal status (default terminated).

    Operator-only and irreversible. Scoped to the caller's customer — never
    touches another tenant's agents.
    """
    ids = (
        await session.execute(
            select(Agent.id).where(
                Agent.customer_id == principal.customer_id,
                Agent.status == payload.status,
            )
        )
    ).scalars().all()
    await _cascade_delete_agents(session, list(ids))
    await session.commit()
    return PruneOut(deleted=len(ids))


async def _cascade_delete_agents(session: AsyncSession, agent_ids: list[UUID]) -> None:
    """Delete agents and their dependents in FK order.

    SQLite (used by the demo / in-process control plane) does not enforce
    `ON DELETE CASCADE` unless `PRAGMA foreign_keys=ON`, so we delete dependents
    explicitly rather than relying on the DB. Order: feedback_tokens (hang off
    kill_events) → kill_events → grants → events → agents.
    """
    if not agent_ids:
        return
    kill_event_ids = (
        await session.execute(
            select(KillEvent.id).where(KillEvent.agent_id.in_(agent_ids))
        )
    ).scalars().all()
    if kill_event_ids:
        await session.execute(
            delete(FeedbackToken).where(
                FeedbackToken.kill_event_id.in_(kill_event_ids)
            )
        )
    await session.execute(delete(KillEvent).where(KillEvent.agent_id.in_(agent_ids)))
    await session.execute(
        delete(ApoptosisGrant).where(ApoptosisGrant.agent_id.in_(agent_ids))
    )
    await session.execute(delete(Event).where(Event.agent_id.in_(agent_ids)))
    await session.execute(delete(Agent).where(Agent.id.in_(agent_ids)))


async def _load_agent_owned_by(
    session: AsyncSession,
    agent_id: UUID,
    customer_id: UUID,
) -> Agent:
    """Fetch an agent and 404 if it doesn't exist or belongs to another customer.

    Returns 404 (not 403) on cross-customer access to avoid leaking the
    existence of other customers' agent_ids.
    """
    stmt = select(Agent).where(Agent.id == agent_id, Agent.customer_id == customer_id)
    result = await session.execute(stmt)
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
    return agent
