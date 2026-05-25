"""Agents router: register, fleet, get one.

M4 adds POST /agents/{id}/terminate and GET /agents/{id}/kill-pending.
"""

from typing import Annotated
from uuid import UUID, uuid4

from caspase.types import (
    AgentRegistrationIn,
    AgentRegistrationOut,
    AgentStatus,
    AgentSummary,
)
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth import Principal, require_principal
from control_plane.db.models import Agent
from control_plane.db.session import get_session

router = APIRouter(prefix="/agents", tags=["agents"])


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
) -> list[AgentSummary]:
    stmt = (
        select(Agent)
        .where(Agent.customer_id == principal.customer_id)
        .order_by(Agent.registered_at.desc())
    )
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
