"""Heartbeat request/response types."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class HeartbeatIn(BaseModel):
    uptime_seconds: float = Field(ge=0)


class HeartbeatOut(BaseModel):
    """Heartbeat ack.

    Carries `active_grants[]` (M5) so the SDK can refresh its in-process
    cache for grant application. The cache is up to one heartbeat-interval
    stale — a grant issued just after a heartbeat won't take effect on the
    SDK until the next one lands. This is acceptable for MVP; if a tighter
    SLO matters we'd need a sidechannel poll like M4 uses for kills.
    """

    received_at: datetime
    active_grants: list[dict[str, Any]] = Field(default_factory=list)
