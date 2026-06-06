"""Policy and threshold types."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import SymptomType


class PolicyThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- loop detection (check 1) ---
    max_loop_repeats: int = Field(default=5, ge=1)
    loop_window_actions: int = Field(default=20, ge=1)
    # Steer (soft intervention) threshold. When the *current* tool call's
    # signature has repeated this many times in the window — but still below
    # `max_loop_repeats` — Hermeskill blocks that one call and injects a
    # corrective "steer" message instead of killing, giving the agent a chance
    # to change approach before apoptosis. `None` disables steering (the agent
    # goes straight to a kill at `max_loop_repeats`, the original behaviour).
    # Must be `< max_loop_repeats` so the steer band has room below the kill.
    loop_steer_repeats: int | None = Field(default=None, ge=1)
    # --- token / cost runaway (check 2) ---
    max_tokens_per_run: int = Field(default=500_000, ge=1)
    max_cost_usd: float = Field(default=25.0, ge=0)
    # --- wall-clock runaway (check 3) ---
    max_runtime_seconds: int = Field(default=1800, ge=1)
    # --- heartbeat + cooperative termination (check 5 + L2 watchdog) ---
    heartbeat_interval_seconds: int = Field(default=30, ge=1)
    cooperative_grace_seconds: int = Field(default=10, ge=1)
    verification_timeout_seconds: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _steer_below_kill(self) -> PolicyThresholds:
        """A steer band only makes sense strictly below the kill threshold.

        If `loop_steer_repeats >= max_loop_repeats` the band is empty (the
        agent would be killed before it could ever be steered), which is almost
        always a misconfiguration — reject it loudly rather than silently
        shipping steering that never fires.
        """
        if (
            self.loop_steer_repeats is not None
            and self.loop_steer_repeats >= self.max_loop_repeats
        ):
            raise ValueError(
                f"loop_steer_repeats ({self.loop_steer_repeats}) must be "
                f"< max_loop_repeats ({self.max_loop_repeats}); the steer band "
                "needs room below the kill threshold"
            )
        return self


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
