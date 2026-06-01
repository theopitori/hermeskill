"""Policy and threshold types."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import SymptomType


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
