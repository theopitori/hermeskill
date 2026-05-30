"""Calibration report types (Phase 4 — feedback-driven threshold suggestions).

These describe a **read-only, advisory** analysis: given the operator feedback
labels collected on past kills (`kill_events.feedback_label`), how well are a
policy's thresholds calibrated? The output is a *suggestion a human applies by
editing the policy* — never an automatic mutation, and never a model that
"learns." The math behind it is a deliberately transparent heuristic; see
`caspase.calibration`.

Honest by construction (this is the whole point of the project): the report
calibrates against **false positives only**. Executed-kill feedback can tell
you whether kills that *fired* were right; it structurally cannot observe kills
that *should* have fired but didn't (no kill_event exists for those). So the
report never recommends tightening — that direction isn't in the data.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import SymptomType


class SymptomCalibration(BaseModel):
    """Per-symptom feedback breakdown + (optional) advisory suggestion."""

    model_config = ConfigDict(extra="forbid")

    symptom: SymptomType
    total_labeled: int = Field(ge=0)
    good_kills: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    missed_kills: int = Field(ge=0)
    other: int = Field(ge=0)
    # false_positives / total_labeled (0.0 when nothing is labeled).
    false_positive_rate: float = Field(ge=0.0, le=1.0)

    # Advisory suggestion. `threshold_field` is None when the symptom maps to
    # no single numeric knob — tool_scope_violation is an allowlist, not a
    # number; heartbeat_stale is a liveness signal, not a feedback-tunable
    # threshold. When set, it names a field on `PolicyThresholds`.
    threshold_field: str | None = None
    current_value: float | None = None
    suggested_value: float | None = None

    # "insufficient_data" | "low" | "medium" | "high"
    confidence: str
    # Human-readable, evidence-first explanation (rate + sample size up front).
    rationale: str


class CalibrationReport(BaseModel):
    """Advisory calibration analysis for a single named policy."""

    model_config = ConfigDict(extra="forbid")

    policy_name: str
    total_labeled_kills: int = Field(ge=0)
    symptoms: list[SymptomCalibration] = Field(default_factory=list)
    # The standing honesty caveat — surfaced verbatim in the CLI footer.
    notes: str = (
        "Advisory only: suggestions are policy edits a human applies, never "
        "auto-tuning. Calibrated against false positives only — kills that "
        "should have fired but didn't aren't observable from executed-kill "
        "feedback, so this never recommends tightening."
    )
