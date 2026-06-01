"""Core relational models: auth and agent lifecycle.

Customer → ApiKey + Agent → Event form one relationship graph; they live
together here so SQLAlchemy can resolve bidirectional Mapped[] annotations
without cross-file circular imports.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ApiKeyRole(StrEnum):
    DEVELOPER = "developer"
    OPERATOR = "operator"


class AgentStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    DYING = "dying"
    TERMINATED = "terminated"
    ZOMBIE = "zombie"


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
