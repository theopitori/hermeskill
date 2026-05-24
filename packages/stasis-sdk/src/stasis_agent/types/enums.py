"""Enumeration types shared across the SDK and control plane."""

from __future__ import annotations

from enum import StrEnum


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
