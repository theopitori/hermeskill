"""Pydantic types shared between SDK and control plane.

Single source of truth — control plane imports the same models via the
workspace dep on `stasis-agent`. Avoid duplicating schemas in
`control_plane.api.*`.

Extended in M1 with the register/heartbeat/events request-response shapes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --- enums ----------------------------------------------------------------


class AgentStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    DYING = "dying"
    TERMINATED = "terminated"
    ZOMBIE = "zombie"


class SymptomType(StrEnum):
    LOOP = "loop"
    TOKEN_RUNAWAY = "token_runaway"
    WALL_CLOCK = "wall_clock"
    TOOL_SCOPE_VIOLATION = "tool_scope_violation"
    HEARTBEAT_STALE = "heartbeat_stale"
    MANUAL_KILL = "manual_kill"


class TriggerType(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class EventType(StrEnum):
    TOOL_CALL = "tool_call"
    LLM_CALL = "llm_call"
    LIFECYCLE = "lifecycle"
    HEARTBEAT = "heartbeat"
    SYMPTOM = "symptom"  # M2 adds these to the lifecycle stream


class FeedbackLabel(StrEnum):
    """Operator's verdict on a kill (M3 one-click feedback).

    Submitted via the unauthenticated POST /feedback/{token} endpoint;
    persisted on `kill_events.feedback_label`. Fixed vocab keeps the
    aggregation cheap — open-ended free-text comments are out of scope
    for MVP.
    """

    GOOD_KILL = "good_kill"
    FALSE_POSITIVE = "false_positive"
    MISSED_KILL = "missed_kill"
    OTHER = "other"


# --- policy --------------------------------------------------------------
#
# Three shipped defaults live in `stasis_agent.policies` (strict /
# coding-default / permissive). Server-side custom-policy persistence and
# the CRUD endpoints land in M5; until then, the SDK is the authoritative
# source of policy contents and the server just stores the name on each
# agent row.
#
# `thresholds` and `apoptosis_proofing` are nested Pydantic models — not
# open dicts — so the M2 symptom checks can read fields with mypy-checked
# attribute access (`state.policy.thresholds.max_loop_repeats`) and typos
# blow up at parse time, not at kill time.


class PolicyThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- loop detection (check 1) ---
    max_loop_repeats: int = Field(default=5, ge=1)
    loop_window_actions: int = Field(default=20, ge=1)
    # --- token / cost runaway (check 2) ---
    max_tokens_per_run: int = Field(default=500_000, ge=1)
    max_cost_usd: float = Field(default=25.0, ge=0)
    # --- wall-clock runaway (check 3) ---
    max_runtime_seconds: int = Field(default=1800, ge=1)
    # --- heartbeat + cooperative termination (check 5 + L2 watchdog) ---
    heartbeat_interval_seconds: int = Field(default=30, ge=1)
    cooperative_grace_seconds: int = Field(default=10, ge=1)
    verification_timeout_seconds: int = Field(default=30, ge=1)


class ApoptosisProofingDefaults(BaseModel):
    """Per-policy defaults that bound what grants may suppress under this policy.

    `allowed_symptoms` is the *grantable* set — symptoms an operator may
    issue an apoptosis-proofing grant for. Manual kill is **never** in
    this list (enforced in apoptosis.terminate(), not in checks.py).
    Resource-burn symptoms (cost, runtime) typically aren't either —
    operators shouldn't be able to grant unlimited spend.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_symptoms: list[SymptomType] = Field(default_factory=list)
    max_duration_hours: int = Field(default=4, ge=1)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    thresholds: PolicyThresholds = Field(default_factory=PolicyThresholds)
    tool_allowlist: list[str] = Field(default_factory=list)
    apoptosis_proofing: ApoptosisProofingDefaults = Field(
        default_factory=ApoptosisProofingDefaults
    )


# --- agents ---------------------------------------------------------------


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


# --- events ---------------------------------------------------------------


class EventIn(BaseModel):
    """One event posted by the SDK (tool call, llm call, lifecycle, etc.)."""

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    # Client-side timestamp; server stamps its own created_at on insert.
    occurred_at: datetime | None = None


class EventBatchIn(BaseModel):
    """SDK batches events for efficient ingest."""

    events: list[EventIn] = Field(default_factory=list, max_length=1000)


class EventOut(BaseModel):
    id: int
    agent_id: UUID
    type: EventType
    payload: dict[str, Any]
    created_at: datetime


class EventPage(BaseModel):
    """Cursor-paginated events response."""

    events: list[EventOut]
    # For paging backwards (default): the smallest id in this page; pass as
    # `before_id` to fetch the next older page.
    next_before_id: int | None = None
    # For tailing (--follow): the largest id seen; pass as `after_id` to poll
    # for events newer than this.
    last_id: int | None = None


# --- heartbeats -----------------------------------------------------------


class HeartbeatIn(BaseModel):
    uptime_seconds: float = Field(ge=0)


class HeartbeatOut(BaseModel):
    """Heartbeat ack.

    In M5 this response carries `active_grants[]` so the SDK can refresh its
    in-process cache for grant application. Empty for now.
    """

    received_at: datetime
    active_grants: list[dict[str, Any]] = Field(default_factory=list)


# --- death certificate ---------------------------------------------------
#
# The death certificate is the most important data the control plane owns.
# It's the forensic record of what killed the agent, when, why, and what
# happened during shutdown. Schema mirrors `death_certificate_jsonb` in
# the plan.
#
# **Death confirmation is decoupled from cert posting.** Cert post can
# fail (network, agent crash) but the agent might still be dead. The
# kill_event status transitions (initiated → confirmed → zombie) are
# driven by heartbeat presence/absence, NOT by cert receipt. See the
# `KillEventStatus` docstring.


class KillEventStatus(StrEnum):
    """Lifecycle of a kill event on the server.

      * **initiated**  — kill decision recorded; agent may or may not have
        died yet. Set by the server when an operator hits /terminate (M4)
        or when the SDK posts an auto-kill cert (this milestone).
      * **confirmed**  — heartbeats have gone silent for ≥3 intervals
        while status was initiated. Strong evidence the agent is dead.
        Set by the server-side sweeper (M4).
      * **zombie**     — heartbeats are STILL arriving past
        `verification_timeout_seconds` while status was initiated.
        Means the SDK heard the kill but the agent didn't die — the
        worst-case operator-alert signal.
    """

    INITIATED = "initiated"
    CONFIRMED = "confirmed"
    ZOMBIE = "zombie"


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


# --- feedback ------------------------------------------------------------


class FeedbackIn(BaseModel):
    """POST body for /feedback/{token}.

    Unauthenticated — the token in the URL is the auth. Single-use:
    a second submission for the same token returns 410.
    """

    model_config = ConfigDict(extra="forbid")

    label: FeedbackLabel


class FeedbackOut(BaseModel):
    """Acknowledgement of a feedback submission."""

    kill_event_id: int
    label: FeedbackLabel
    received_at: datetime


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


# --- manual kill (M4) ---------------------------------------------------


class TerminateAgentIn(BaseModel):
    """POST body for /agents/{id}/terminate.

    `reason` is the operator's free-form justification. It's persisted on
    `kill_events.operator_reason` and surfaces in the death cert + the
    `stasis logs` view, so write something a human will read.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


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
