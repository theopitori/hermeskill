"""Kill-events router — the death certificate endpoint (M2.5).

`POST /agents/{id}/kill_events` is how the SDK reports an agent's death.
For the M2 auto-kill path this is straightforward: the SDK detects a
Terminal symptom, the agent dies cooperatively, the SDK posts this
endpoint with the full death certificate. The server writes a
`kill_events` row, marks the agent as TERMINATED, and returns the row.

For the M4 manual-kill path the operator hits `POST /agents/{id}/terminate`
first; that creates a kill_event with status='initiated'. When the SDK
then sees the kill via the poll loop, the agent dies, and the SDK posts
this endpoint — which finds the existing row and UPDATEs it with the
cert + shutdown log rather than inserting a new one. That code path is
implemented here already so the manual flow only needs the /terminate
endpoint added in M4.

**The partial unique constraint** (`ux_kill_events_one_active_per_agent`,
defined in migration 0002) prevents two kill_events being active for
the same agent — a symptom-kill racing against a manual-kill. On race,
this endpoint returns 409 with the existing kill_event id in the body;
the SDK is expected to treat 409 as "already dying, fine" and stop.

GET endpoints (`GET /kill_events/{id}` and `GET /agents/{id}/kill_events`)
are read-only forensics for the CLI and future dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from caspase.types import KillEventIn, KillEventOut
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api._kill_event_serializers import _kill_event_out
from control_plane.api.agents import _load_agent_owned_by
from control_plane.auth import Principal, require_principal

# Use the DB-side enums (not the SDK contract enums from caspase.types)
# for attribute writes — SQLAlchemy's Mapped[Enum] columns are strictly
# typed against the enum imported into the model module. Their members
# carry identical string values so wire-level behavior is unchanged.
from control_plane.db.models import (
    Agent,
    AgentStatus,
    FeedbackToken,
    KillEvent,
    KillEventStatus,
)
from control_plane.db.session import get_session
from control_plane.feedback_tokens import build_feedback_url, generate_feedback_token
from control_plane.settings import settings

router = APIRouter(prefix="/agents", tags=["kill_events"])
top_router = APIRouter(prefix="/kill_events", tags=["kill_events"])


async def _existing_active_kill_event(
    session: AsyncSession, agent_id: UUID
) -> KillEvent | None:
    """Return the active (INITIATED or CONFIRMED) kill_event for this agent, or None."""
    stmt = select(KillEvent).where(
        KillEvent.agent_id == agent_id,
        KillEvent.status.in_([KillEventStatus.INITIATED, KillEventStatus.CONFIRMED]),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _build_or_update_kill_event(
    session: AsyncSession,
    payload: KillEventIn,
    agent_id: UUID,
    existing: KillEvent | None,
    cert_dump: dict[str, object],
    shutdown_dump: list[dict[str, object]],
) -> KillEvent:
    """INSERT a new kill_event or UPDATE the existing INITIATED row.

    cert_dump must already contain `feedback_url` — the caller sets it
    before this call to avoid the SQLAlchemy JSONB in-place-mutation trap.
    """
    if existing is not None:
        existing.terminated_at = payload.terminated_at
        existing.death_certificate = cert_dump
        existing.shutdown_log = shutdown_dump
        existing.status = KillEventStatus.CONFIRMED
        return existing
    kill_event = KillEvent(
        agent_id=agent_id,
        trigger_type=payload.trigger_type,
        trigger_reason=payload.trigger_reason,
        triggered_at=payload.triggered_at,
        terminated_at=payload.terminated_at,
        status=KillEventStatus.CONFIRMED,
        death_certificate=cert_dump,
        shutdown_log=shutdown_dump,
    )
    session.add(kill_event)
    return kill_event


def _add_feedback_token(
    session: AsyncSession, kill_event_id: int, token_hash: str
) -> None:
    """Attach a feedback token row to a freshly flushed kill_event."""
    session.add(
        FeedbackToken(
            token_hash=token_hash,
            kill_event_id=kill_event_id,
            expires_at=datetime.now(UTC)
            + timedelta(days=settings.feedback_token_ttl_days),
        )
    )


async def _409_for_active_kill(
    session: AsyncSession, agent_id: UUID
) -> HTTPException:
    """Build a 409 after an IntegrityError on kill_events or feedback_tokens.

    Two callers: partial unique index on kill_events (symptom vs manual
    race) and unique on feedback_tokens.kill_event_id (token double-issue).
    Both surfaces the existing active kill_event id so the SDK can correlate.
    """
    winner = await _existing_active_kill_event(session, agent_id)
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "detail": "agent kill_event already in flight",
            "existing_kill_event_id": winner.id if winner else None,
        },
    )


@router.post(
    "/{agent_id}/kill_events",
    status_code=status.HTTP_201_CREATED,
    response_model=KillEventOut,
    responses={
        409: {
            "description": "Agent already has an active kill_event; the SDK "
            "should treat this as 'already dying, fine'."
        }
    },
)
async def create_kill_event(
    agent_id: UUID,
    payload: KillEventIn,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> KillEventOut:
    """Record an agent's death.

    Two paths:
      * **No existing kill_event** (auto path, M2): INSERT a new row with
        status=CONFIRMED (SDK posting the cert IS the strongest signal
        the agent reached death — the per-status sweeper is an extra
        layer for cases where the cert never arrives).
      * **Existing row with status=INITIATED** (manual path, M4): UPDATE
        with the cert + shutdown log + terminated_at; promote status to
        CONFIRMED.
      * **Existing row with status=CONFIRMED or ZOMBIE**: 409 with the
        existing id. SDK stops.

    All three paths also flip the agent's status to TERMINATED. After
    this returns, `GET /agents/{id}` shows the agent as terminated.
    """
    agent = await _load_agent_owned_by(session, agent_id, principal.customer_id)
    existing = await _existing_active_kill_event(session, agent_id)

    if existing is not None and existing.status == KillEventStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "agent already has a confirmed kill_event",
                "existing_kill_event_id": existing.id,
            },
        )

    cert_dump = payload.death_certificate.model_dump(mode="json")
    shutdown_dump = [e.model_dump(mode="json") for e in payload.shutdown_log]

    # Inject the feedback URL into cert_dump BEFORE passing it to the
    # builder — SQLAlchemy won't detect mutations to an already-assigned
    # JSONB column dict.
    raw_token, token_hash = generate_feedback_token()
    cert_dump["feedback_url"] = build_feedback_url(settings.feedback_base_url, raw_token)

    kill_event = _build_or_update_kill_event(
        session, payload, agent_id, existing, cert_dump, shutdown_dump
    )
    agent.status = AgentStatus.TERMINATED
    agent.terminated_at = payload.terminated_at

    try:
        await session.flush()
        _add_feedback_token(session, kill_event.id, token_hash)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise await _409_for_active_kill(session, agent_id) from exc

    await session.refresh(kill_event)
    response.status_code = status.HTTP_201_CREATED
    return _kill_event_out(kill_event)


@router.get("/{agent_id}/kill_events", response_model=list[KillEventOut])
async def list_kill_events_for_agent(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> list[KillEventOut]:
    """Forensic timeline: every kill_event for this agent, most recent first.

    Returns an empty list if the agent has never died. Used by the CLI
    (`caspase logs <id>` will surface the death cert if present) and the
    future dashboard.
    """
    await _load_agent_owned_by(session, agent_id, principal.customer_id)  # 404 check
    stmt = (
        select(KillEvent)
        .where(KillEvent.agent_id == agent_id)
        .order_by(KillEvent.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_kill_event_out(r) for r in rows]


@top_router.get("/{kill_event_id}", response_model=KillEventOut)
async def get_kill_event(
    kill_event_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> KillEventOut:
    """Operator-facing read for a single death cert by id.

    Enforces ownership via the agent's customer — 404 (not 403) on a
    cross-customer id to avoid leaking existence.
    """
    stmt = (
        select(KillEvent)
        .join(Agent, Agent.id == KillEvent.agent_id)
        .where(
            KillEvent.id == kill_event_id,
            Agent.customer_id == principal.customer_id,
        )
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kill_event not found",
        )
    return _kill_event_out(row)
