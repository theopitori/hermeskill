"""API-key authentication.

Customer sends `Authorization: Bearer sk_…`. We SHA-256 the raw key, look it up
in `api_keys.key_hash` (rejecting any with `revoked_at` set), and return a
`Principal` carrying the api_key_id, customer_id, and role. The id field on
Principal is what M4 stamps onto `kill_events.operator_id` for audit.

Hashing is done at every request — no in-process cache yet. Once profiling
shows it matters we can add a small TTL cache; for the MVP it's not the
bottleneck.
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import ApiKey
from control_plane.db.session import get_session


class Role(StrEnum):
    DEVELOPER = "developer"
    OPERATOR = "operator"


class Principal(BaseModel):
    """Authenticated caller identity attached to a request."""

    api_key_id: UUID
    customer_id: UUID
    role: Role


def hash_api_key(raw: str) -> str:
    """SHA-256 hex digest of the raw API key string."""
    return sha256(raw.encode("utf-8")).hexdigest()


async def require_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw = authorization.removeprefix("Bearer ").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    key_hash = hash_api_key(raw)
    stmt = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
    result = await session.execute(stmt)
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return Principal(api_key_id=key.id, customer_id=key.customer_id, role=Role(key.role))


def require_operator(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    if principal.role is not Role.OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator role required",
        )
    return principal


# Endpoints use the inline form `Annotated[Principal, Depends(require_principal)]`
# rather than a TypeAlias. `from __future__ import annotations` + 3.14's lazy
# annotation evaluation breaks FastAPI's introspection through a type alias —
# FastAPI sees the alias name and treats it as a plain class parameter.
