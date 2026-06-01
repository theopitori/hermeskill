"""Death certificate, kill event, and termination types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import KillEventStatus, TriggerType


class ShutdownLogEntry(BaseModel):
    """One step in the agent's apoptosis sequence.

    Appended by `WatcherState.record_shutdown_step()` during the death
    sequence. Goes into the death cert under `shutdown_log`.
    """

    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1, max_length=80)
    at: datetime
    duration_ms: float | None = Field(default=None, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class DeathCertificate(BaseModel):
    """The forensic record of an agent's death.

    The SDK builds this in the auto-kill path; the server stores it on
    the `kill_events` row. `customer_id`, `policy_id`, and `feedback_url`
    are server-filled (the SDK doesn't know them or shouldn't be
    authoritative).
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    triggered_at: datetime
    terminated_at: datetime
    trigger_type: TriggerType
    trigger_reason: str = Field(min_length=1)
    # Symptom events captured from `state.symptoms_log`.
    symptoms_log: list[dict[str, Any]] = Field(default_factory=list)
    # Customer-facing snapshot of what the agent was doing at end-of-life.
    # Populated from cleanup hooks in v2 — empty by default for MVP.
    final_state: dict[str, Any] = Field(default_factory=dict)
    shutdown_log: list[ShutdownLogEntry] = Field(default_factory=list)
    # M4 fills these for manual kills.
    operator: str | None = None
    operator_reason: str | None = None
    # Server-filled at cert-insert time (M3). The SDK posts this as None;
    # the server mints a feedback token, stores its hash on `feedback_tokens`,
    # and injects the public click-through URL here before returning the cert.
    feedback_url: str | None = None


class KillEventIn(BaseModel):
    """POST body when the SDK reports a kill to the server.

    For the auto-kill path (M2.5): SDK detects a Terminal symptom, agent
    dies cooperatively, SDK posts this with `trigger_type=auto` and the
    full death cert. Server inserts a `kill_events` row, sets agent
    status to TERMINATED, returns the assigned id.
    """

    model_config = ConfigDict(extra="forbid")

    trigger_type: TriggerType
    trigger_reason: str = Field(min_length=1)
    triggered_at: datetime
    terminated_at: datetime
    death_certificate: DeathCertificate
    shutdown_log: list[ShutdownLogEntry] = Field(default_factory=list)


class KillEventOut(BaseModel):
    """A kill event as the operator sees it (GET /kill_events/{id})."""

    id: int
    agent_id: UUID
    trigger_type: TriggerType
    trigger_reason: str
    status: KillEventStatus
    triggered_at: datetime
    terminated_at: datetime | None
    death_certificate: DeathCertificate | None
    shutdown_log: list[ShutdownLogEntry]
    operator_reason: str | None
    created_at: datetime


class KillEventConflict(BaseModel):
    """Body returned on 409 — agent already has an active kill_event.

    The SDK reads `existing_kill_event_id` and treats it as 'someone else
    already filed the kill cert; we're fine'. M4 manual kill races against
    M2 auto kill via this path.
    """

    detail: str
    existing_kill_event_id: int


class TerminateAgentIn(BaseModel):
    """POST body for /agents/{id}/terminate.

    `reason` is the operator's free-form justification. It's persisted on
    `kill_events.operator_reason` and surfaces in the death cert + the
    `hermeskill logs` view, so write something a human will read.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
