"""ApoptosisGrant model (M5)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ApoptosisGrant(Base):
    """Operator-issued permission for an agent to survive specific symptoms.

    Active = `revoked_at IS NULL AND expires_at > now()`. Inactive rows are
    kept for audit (the death cert may reference suppressed warnings by
    grant_id). Validation (symptom-subset, max duration, no manual_kill)
    lives in the API layer, not the model — see [api/grants.py].
    """

    __tablename__ = "apoptosis_grants"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # JSONB list of SymptomType string values.
    symptoms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    issued_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
