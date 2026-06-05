"""KillEvent model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class KillEventStatus(StrEnum):
    """Mirrors `hermeskill.types.KillEventStatus`. See migration 0002 for
    the CHECK constraint that enforces this set on the DB side."""

    INITIATED = "initiated"
    CONFIRMED = "confirmed"
    ZOMBIE = "zombie"


class TriggerType(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class KillEvent(Base):
    """The death certificate row — the most important data this service owns.

    See `migrations/versions/0002_kill_events.py` for the schema and the
    partial unique constraint that prevents symptom-vs-manual kill races.
    """

    __tablename__ = "kill_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_type: Mapped[TriggerType] = mapped_column(String(20), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[KillEventStatus] = mapped_column(
        String(20),
        default=KillEventStatus.INITIATED,
        nullable=False,
    )
    operator_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )
    operator_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    death_certificate: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    shutdown_log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    feedback_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    feedback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
