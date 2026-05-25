"""One-click feedback endpoint (M3).

Unauthenticated by design — the token in the URL **is** the auth. The raw
token is embedded in the death certificate at issue time
([control_plane.api.kill_events]). On submission we hash the raw token
(see [control_plane.feedback_tokens.hash_feedback_token]) before the
SELECT — the symmetric-hash invariant from TODO.md #9.

Semantics:
  * 404 — token not found or past `expires_at`
  * 410 — token already used (single-use; second submission rejected)
  * 200 — first submission; persists `feedback_label` + `feedback_at`
          on the associated `kill_events` row

The kill_event update happens in the same transaction as the
`feedback_tokens.used_at` stamp so the two states can't drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from caspase.types import FeedbackIn, FeedbackOut
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import FeedbackToken, KillEvent
from control_plane.db.session import get_session
from control_plane.feedback_tokens import hash_feedback_token

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post(
    "/{raw_token}",
    response_model=FeedbackOut,
    responses={
        404: {"description": "Token not found or expired"},
        410: {"description": "Token already used"},
    },
)
async def submit_feedback(
    raw_token: str,
    payload: FeedbackIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FeedbackOut:
    """Record an operator's verdict on a kill.

    Public endpoint. Hashes the URL's raw token before the SELECT so a
    raw token never appears in a query log.
    """
    token_hash = hash_feedback_token(raw_token)

    stmt = select(FeedbackToken).where(FeedbackToken.token_hash == token_hash)
    token_row = (await session.execute(stmt)).scalar_one_or_none()

    now = datetime.now(UTC)
    if token_row is None or token_row.expires_at <= now:
        # Same response for "never existed" and "expired" — don't leak
        # whether the token was ever real to a stranger.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="feedback token not found",
        )
    if token_row.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="feedback already submitted for this kill",
        )

    kill_event_stmt = select(KillEvent).where(
        KillEvent.id == token_row.kill_event_id
    )
    kill_event = (await session.execute(kill_event_stmt)).scalar_one_or_none()
    if kill_event is None:
        # CASCADE should keep these in sync — but guard rather than 500.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="feedback token not found",
        )

    token_row.used_at = now
    kill_event.feedback_label = payload.label.value
    kill_event.feedback_at = now

    await session.commit()

    return FeedbackOut(
        kill_event_id=kill_event.id,
        label=payload.label,
        received_at=now,
    )
