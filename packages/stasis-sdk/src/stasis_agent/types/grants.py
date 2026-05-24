"""Grant request/response types (M5 apoptosis-proofing)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import SymptomType


class GrantIn(BaseModel):
    """POST body for `/agents/{id}/grants`.

    Operator-issued. The server validates `symptoms` against the agent's
    policy `apoptosis_proofing.allowed_symptoms`, rejects `manual_kill`
    unconditionally, and caps `duration_seconds` at 86_400 (24h). These
    rules are policy- and design-level invariants — the wire format only
    carries the request itself.
    """

    model_config = ConfigDict(extra="forbid")

    symptoms: list[SymptomType] = Field(min_length=1)
    duration_seconds: int = Field(ge=60, le=86_400)
    reason: str = Field(min_length=1, max_length=2000)


class GrantRevokeIn(BaseModel):
    """POST body for `/grants/{id}/revoke`."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class GrantOut(BaseModel):
    """A grant as the operator + SDK see it.

    `active` is computed server-side: True iff `revoked_at is None and
    expires_at > now()`. The SDK reads this off the heartbeat response;
    operators read it via `stasis grant`/`stasis fleet`.
    """

    id: UUID
    agent_id: UUID
    symptoms: list[SymptomType]
    reason: str
    issued_by: UUID | None
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: UUID | None
    revoke_reason: str | None
    active: bool


class PendingKillOut(BaseModel):
    """One entry in `GET /kills/pending` — a kill the SDK should act on.

    The SDK's poller maps each entry to a local `WatcherState`, stashes
    the operator context on `state.manual_kill`, then calls
    `request_termination()` so the L1 cooperative gate fires at the next
    checkpoint.
    """

    agent_id: UUID
    kill_event_id: int
    trigger_reason: str
    triggered_at: datetime
    operator_reason: str | None = None
    # Identifier of the operator's api_key (audit; SDK never authenticates
    # against it). Stamped into the death cert as `operator`.
    operator: str | None = None
