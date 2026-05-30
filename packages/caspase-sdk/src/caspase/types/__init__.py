"""Pydantic types shared between SDK and control plane.

Single source of truth — control plane imports the same models via the
workspace dep on `caspase`. Avoid duplicating schemas in
`control_plane.api.*`.

Extended in M1 with the register/heartbeat/events request-response shapes.
"""

from __future__ import annotations

from .agents import AgentRegistrationIn, AgentRegistrationOut, AgentSummary
from .calibration import CalibrationReport, SymptomCalibration
from .enums import (
    AgentStatus,
    EventType,
    FeedbackLabel,
    KillEventStatus,
    SymptomType,
    TriggerType,
)
from .events import EventBatchIn, EventIn, EventOut, EventPage
from .feedback import FeedbackIn, FeedbackOut
from .grants import GrantIn, GrantOut, GrantRevokeIn, PendingKillOut
from .heartbeats import HeartbeatIn, HeartbeatOut
from .kills import (
    DeathCertificate,
    KillEventConflict,
    KillEventIn,
    KillEventOut,
    ShutdownLogEntry,
    TerminateAgentIn,
)
from .policy import ApoptosisProofingDefaults, Policy, PolicyThresholds

__all__ = [
    "AgentRegistrationIn",
    "AgentRegistrationOut",
    "AgentStatus",
    "AgentSummary",
    "ApoptosisProofingDefaults",
    "CalibrationReport",
    "DeathCertificate",
    "EventBatchIn",
    "EventIn",
    "EventOut",
    "EventPage",
    "EventType",
    "FeedbackIn",
    "FeedbackLabel",
    "FeedbackOut",
    "GrantIn",
    "GrantOut",
    "GrantRevokeIn",
    "HeartbeatIn",
    "HeartbeatOut",
    "KillEventConflict",
    "KillEventIn",
    "KillEventOut",
    "KillEventStatus",
    "PendingKillOut",
    "Policy",
    "PolicyThresholds",
    "ShutdownLogEntry",
    "SymptomCalibration",
    "SymptomType",
    "TerminateAgentIn",
    "TriggerType",
]
