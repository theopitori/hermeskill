"""Agent registration and summary types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from .enums import AgentStatus


class AgentRegistrationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    policy_name: str = Field(min_length=1, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRegistrationOut(BaseModel):
    agent_id: UUID
    policy_name: str
    registered_at: datetime


class AgentSummary(BaseModel):
    id: UUID
    name: str
    policy_name: str
    status: AgentStatus
    registered_at: datetime
    last_heartbeat_at: datetime | None
    terminated_at: datetime | None
