"""SQLAlchemy 2.0 declarative models.

M1 lands the foundational tables: customers, api_keys, agents, events. M2.5
adds kill_events. M3 adds feedback_tokens. Later milestones add: symptoms
(M2), apoptosis_grants (M5). Webhooks were deferred from MVP.

ID convention: 16-byte UUIDv7 stored as Postgres `uuid`. UUIDv7 is monotonic
(time-prefixed) so it indexes well for our access patterns ("most recent
events for agent X").
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all Stasis control-plane models."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB().with_variant(JSON(), "sqlite"),
    }


# ---------------------------------------------------------------------------
# enums (mirror stasis_agent.types so DBA can read schema without SDK)
# ---------------------------------------------------------------------------


class ApiKeyRole(StrEnum):
    DEVELOPER = "developer"
    OPERATOR = "operator"


class AgentStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    DYING = "dying"
    TERMINATED = "terminated"
    ZOMBIE = "zombie"


class KillEventStatus(StrEnum):
    """Mirrors `stasis_agent.types.KillEventStatus`. See migration 0002 for
    the CHECK constraint that enforces this set on the DB side."""

    INITIATED = "initiated"
    CONFIRMED = "confirmed"
    ZOMBIE = "zombie"


class TriggerType(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="customer", cascade="all,delete")
    agents: Mapped[list[Agent]] = relationship(back_populates="customer", cascade="all,delete")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[ApiKeyRole] = mapped_column(String(20), nullable=False)
    # SHA-256 hex of the raw key string. Raw key only shown at creation time.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="api_keys")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # policy is a string name for now; the policies table lands in M5 when grants need it.
    policy_name: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        String(20),
        default=AgentStatus.REGISTERED,
        nullable=False,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="agents")
    events: Mapped[list[Event]] = relationship(back_populates="agent", cascade="all,delete")


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


class FeedbackToken(Base):
    """One-click feedback token (M3).

    PK is the SHA-256 hex of the raw token — same posture as `api_keys.key_hash`.
    Raw tokens only appear in the feedback URL embedded in the death cert;
    they are never persisted. `kill_event_id` is UNIQUE so a cert has exactly
    one token. Single-use: `used_at` is stamped on the first successful POST
    /feedback/{token}; subsequent submissions return 410.
    """

    __tablename__ = "feedback_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kill_event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kill_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # discriminator: tool_call, llm_call, heartbeat, lifecycle, symptom
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    agent: Mapped[Agent] = relationship(back_populates="events")

    __table_args__ = (
        # Hot path: "give me the last N events for this agent."
        Index("ix_events_agent_id_id_desc", "agent_id", text("id DESC")),
    )
