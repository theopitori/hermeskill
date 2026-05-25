"""Apoptosis-proofing grants (M5).

A grant is an operator's signed permission for an agent to survive a
specific set of symptoms for a bounded duration. The SDK caches active
grants via the heartbeat response and demotes covered Terminal symptoms
into Warnings before the apoptosis flag is set.

Server-side validation (this module):
  * `symptoms ⊆ policy.apoptosis_proofing.allowed_symptoms`. Looked up
    via the SDK's `resolve_policy` against the agent's `policy_name`.
    Custom server-side policies are deferred from M5; this works for
    every customer using a shipped policy.
  * `SymptomType.MANUAL_KILL` is **never** grantable, regardless of
    policy. Defense-in-depth: an operator must be able to override a
    grant by killing the agent (M4).
  * `duration_seconds <= 86_400` (24h hard cap on top of any
    policy-allowed `max_duration_hours`).

Manual-kill bypass is automatic: M4's poller calls
`state.request_termination()` directly, which doesn't go through
`apply_grants`. No special-case code needed here.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from caspase.policies import UnknownPolicyError, resolve_policy
from caspase.types import (
    GrantIn,
    GrantOut,
    GrantRevokeIn,
    SymptomType,
)
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.agents import _load_agent_owned_by
from control_plane.auth import Principal, require_operator, require_principal
from control_plane.db.models import Agent, ApoptosisGrant
from control_plane.db.session import get_session

# Two routers — nested under /agents for create + list, top-level for
# revoke (the operator addresses a grant by its own id once issued).
router = APIRouter(prefix="/agents", tags=["grants"])
top_router = APIRouter(prefix="/grants", tags=["grants"])

# Hard ceiling regardless of policy. The Pydantic schema already enforces
# this on the wire; we re-assert here so a custom-policy world (deferred)
# can't accidentally extend it.
MAX_DURATION_SECONDS = 86_400  # 24h


@router.post(
    "/{agent_id}/grants",
    status_code=status.HTTP_201_CREATED,
    response_model=GrantOut,
    responses={
        403: {"description": "operator role required"},
        404: {"description": "agent not found"},
        422: {"description": "grant violates policy or hard caps"},
    },
)
async def create_grant(
    agent_id: UUID,
    payload: GrantIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator)],
) -> GrantOut:
    """Issue a new grant for an agent.

    Validation order — fast rejects first:
      1. `manual_kill` in symptoms → 422 (universal rule).
      2. Resolve agent's policy → 422 if unknown (custom policies not yet
         supported).
      3. `set(symptoms) ⊆ allowed_symptoms` for that policy → 422.
      4. `duration_seconds <= MAX_DURATION_SECONDS` → already enforced by
         Pydantic but re-asserted for the audit message.

    The grant takes effect on the SDK at the next heartbeat (default 30s).
    Up to one heartbeat-interval of staleness is acceptable — operators
    are explicitly making a "let it keep running" decision, not racing
    a millisecond cliff.
    """
    agent = await _load_agent_owned_by(session, agent_id, principal.customer_id)

    if SymptomType.MANUAL_KILL in payload.symptoms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manual_kill is never grantable",
        )

    try:
        policy = resolve_policy(agent.policy_name)
    except UnknownPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"cannot validate grant: unknown policy {agent.policy_name!r}. "
                "Custom server-side policies are not yet supported."
            ),
        ) from exc

    allowed = set(policy.apoptosis_proofing.allowed_symptoms)
    requested = set(payload.symptoms)
    disallowed = requested - allowed
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"policy {agent.policy_name!r} does not allow grants for: "
                f"{sorted(s.value for s in disallowed)}. "
                f"Allowed: {sorted(s.value for s in allowed)}."
            ),
        )

    if payload.duration_seconds > MAX_DURATION_SECONDS:
        # Pydantic already enforces this on the wire; re-asserted so a
        # bypassed validator can't slip through.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"duration_seconds exceeds the {MAX_DURATION_SECONDS}s hard cap",
        )

    now = datetime.now(UTC)
    grant = ApoptosisGrant(
        id=uuid4(),
        agent_id=agent_id,
        symptoms=[s.value for s in payload.symptoms],
        reason=payload.reason,
        issued_by=principal.api_key_id,
        expires_at=now + timedelta(seconds=payload.duration_seconds),
    )
    session.add(grant)
    await session.commit()
    await session.refresh(grant)
    return _grant_out(grant, now=now)


@router.get(
    "/{agent_id}/grants",
    response_model=list[GrantOut],
)
async def list_grants_for_agent(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
    active_only: bool = False,
) -> list[GrantOut]:
    """All grants ever issued for this agent, newest first.

    `active_only=true` filters to `revoked_at IS NULL AND expires_at > now()`,
    which is what the heartbeat enrichment uses.
    """
    await _load_agent_owned_by(session, agent_id, principal.customer_id)
    stmt = (
        select(ApoptosisGrant)
        .where(ApoptosisGrant.agent_id == agent_id)
        .order_by(ApoptosisGrant.issued_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    now = datetime.now(UTC)
    outs = [_grant_out(r, now=now) for r in rows]
    if active_only:
        return [g for g in outs if g.active]
    return outs


@top_router.post("/{grant_id}/revoke", response_model=GrantOut)
async def revoke_grant(
    grant_id: UUID,
    payload: GrantRevokeIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator)],
) -> GrantOut:
    """Stamp `revoked_at` + `revoked_by` on a grant. Idempotent.

    A second revoke of the same grant returns 200 with the unchanged
    row — same posture as kill-event 409 (no error for an operator
    acting twice on the same target).
    """
    # Ownership check piggy-backs on the agent FK.
    stmt = (
        select(ApoptosisGrant, Agent)
        .join(Agent, Agent.id == ApoptosisGrant.agent_id)
        .where(
            ApoptosisGrant.id == grant_id,
            Agent.customer_id == principal.customer_id,
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="grant not found",
        )
    grant: ApoptosisGrant = row[0]

    now = datetime.now(UTC)
    if grant.revoked_at is None:
        grant.revoked_at = now
        grant.revoked_by = principal.api_key_id
        grant.revoke_reason = payload.reason
        await session.commit()
        await session.refresh(grant)
    return _grant_out(grant, now=now)


def _grant_out(row: ApoptosisGrant, *, now: datetime) -> GrantOut:
    active = row.revoked_at is None and row.expires_at > now
    return GrantOut(
        id=row.id,
        agent_id=row.agent_id,
        symptoms=[SymptomType(s) for s in row.symptoms],
        reason=row.reason,
        issued_by=row.issued_by,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revoke_reason=row.revoke_reason,
        active=active,
    )


async def load_active_grants(
    session: AsyncSession, agent_id: UUID
) -> list[dict[str, object]]:
    """Helper for the heartbeat endpoint — returns the active grants
    for `agent_id` in the wire shape the SDK expects.

    "Active" = `revoked_at IS NULL AND expires_at > now()`. Returned as
    plain dicts because `HeartbeatOut.active_grants` is `list[dict]`
    today (we'd tighten that to `list[GrantOut]` here if the SDK ever
    needs the full operator audit fields on heartbeat — it doesn't).
    """
    now = datetime.now(UTC)
    stmt = (
        select(ApoptosisGrant)
        .where(
            ApoptosisGrant.agent_id == agent_id,
            ApoptosisGrant.revoked_at.is_(None),
            ApoptosisGrant.expires_at > now,
        )
        .order_by(ApoptosisGrant.issued_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "symptoms": list(r.symptoms),
            "expires_at": r.expires_at.isoformat(),
            "reason": r.reason,
        }
        for r in rows
    ]
