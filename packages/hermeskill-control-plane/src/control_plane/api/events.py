"""Events router: batched ingest from the SDK, paginated read for `hermeskill logs`.

Read pagination supports two modes:
- **Paging backwards** (`before_id` cursor): default, descending order. Used
  for "show me the last N events" and historical browsing.
- **Tailing** (`after_id` cursor): ascending order. Used by
  `hermeskill logs --follow` to poll for events newer than the last one seen.
"""

from typing import Annotated
from uuid import UUID

from hermeskill.types import EventBatchIn, EventOut, EventPage, EventType
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.agents import _load_agent_owned_by
from control_plane.auth import Principal, require_principal
from control_plane.db.models import Event
from control_plane.db.session import get_session

router = APIRouter(prefix="/agents", tags=["events"])


@router.post(
    "/{agent_id}/events",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=dict[str, int],
)
async def post_events(
    agent_id: UUID,
    batch: EventBatchIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, int]:
    agent = await _load_agent_owned_by(session, agent_id, principal.customer_id)
    if not batch.events:
        return {"accepted": 0}
    # Bulk insert — let Postgres assign the autoincrement ids.
    session.add_all(
        Event(agent_id=agent.id, type=e.type.value, payload=e.payload) for e in batch.events
    )
    await session.commit()
    return {"accepted": len(batch.events)}


@router.get(
    "/{agent_id}/events",
    response_model=EventPage,
)
async def list_events(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
    limit: int = Query(default=100, ge=1, le=1000),
    before_id: int | None = Query(default=None, ge=0),
    after_id: int | None = Query(default=None, ge=0),
) -> EventPage:
    if before_id is not None and after_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="specify at most one of before_id or after_id",
        )
    await _load_agent_owned_by(session, agent_id, principal.customer_id)

    stmt = select(Event).where(Event.agent_id == agent_id)
    if after_id is not None:
        # Tail mode: ascending, events newer than the cursor.
        stmt = stmt.where(Event.id > after_id).order_by(Event.id.asc()).limit(limit)
    else:
        # Default / paging-back mode: descending.
        if before_id is not None:
            stmt = stmt.where(Event.id < before_id)
        stmt = stmt.order_by(Event.id.desc()).limit(limit)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    events = [
        EventOut(
            id=r.id,
            agent_id=r.agent_id,
            type=EventType(r.type),
            payload=r.payload,
            created_at=r.created_at,
        )
        for r in rows
    ]

    if after_id is not None:
        # Ascending — last_id is the largest, useful for the next poll.
        tail_last_id = events[-1].id if events else after_id
        return EventPage(events=events, last_id=tail_last_id)
    # Descending — next page cursor is the smallest id in this page.
    next_before = events[-1].id if len(events) == limit else None
    head_last_id = events[0].id if events else None
    return EventPage(events=events, next_before_id=next_before, last_id=head_last_id)
