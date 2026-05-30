"""Feedback-driven threshold calibration (Phase 4).

The control plane already collects an operator's verdict on every kill via the
one-click feedback link baked into each death certificate
(`kill_events.feedback_label`). Until now those labels just sat in the database.
This module turns them into a **transparent, advisory** calibration report: per
symptom, how often did kills under a given policy get labeled false-positive,
and — if that rate is high enough on a large enough sample — what looser
threshold should a human *consider* setting.

Design constraints (these are deliberate, and they are the point):

  * **Suggest-only.** We never mutate a policy. Policies are SDK-defined
    constants (`caspase.policies`); the "suggestion" is literally "edit that
    constant." Auto-tuning limits from agent-influenced feedback would be both
    an overclaim and a genuine safety hole.
  * **No learning / no ML.** It's a rate, a sample-size gate, and one fixed
    conservative step. A reviewer can read the whole rule in a minute and
    trust it precisely because there's no black box.
  * **Evidence over precision.** The suggested number is a conservative nudge
    (`* 1.5`, rounded to something readable). What should actually drive the
    decision is the evidence we lead with: the false-positive rate and n.
  * **False positives only.** See `caspase.types.calibration` — the data can't
    speak to kills that never fired, so we never recommend tightening.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from caspase.types import (
    CalibrationReport,
    FeedbackLabel,
    Policy,
    SymptomCalibration,
    SymptomType,
)

# --- tunables (transparent, documented) ----------------------------------

#: Below this many labeled kills for a symptom, we report stats but make no
#: suggestion — a 1-of-1 false positive is noise, not a signal.
MIN_SAMPLES_PER_SYMPTOM = 5

#: Only suggest loosening when at least this fraction of a symptom's labeled
#: kills were false positives. 30% wrong is a real calibration problem.
FALSE_POSITIVE_ACTION_THRESHOLD = 0.30

#: The fixed conservative step. We loosen by half, then round to a readable
#: value. Intentionally *not* derived from the false-positive rate — a
#: rate-scaled number ("3 → 4.2") reads as false precision; a flat nudge plus
#: the evidence reads as honest.
LOOSEN_FACTOR = 1.5


@dataclass(frozen=True)
class _Knob:
    """A symptom's single numeric threshold, and how to round a suggestion."""

    field: str
    kind: str  # "int" | "seconds" | "usd"


#: Symptoms that map to exactly one numeric threshold worth suggesting. The
#: others (tool_scope_violation → allowlist, heartbeat_stale → liveness,
#: manual_kill → operator) have no single knob and are reported stats-only.
_SYMPTOM_KNOB: dict[SymptomType, _Knob] = {
    SymptomType.LOOP: _Knob("max_loop_repeats", "int"),
    SymptomType.TOKEN_RUNAWAY: _Knob("max_cost_usd", "usd"),
    SymptomType.WALL_CLOCK: _Knob("max_runtime_seconds", "seconds"),
}


@dataclass(frozen=True)
class LabeledKill:
    """One past kill plus the operator's verdict on it.

    The minimal input the calibrator needs. The control plane builds these
    from `kill_events` rows (symptom extracted from the death cert's terminal
    `symptoms_log` entry, label from `feedback_label`); tests build them
    directly. Unlabeled kills are simply not passed in.
    """

    symptom: SymptomType
    label: FeedbackLabel


def _loosen(current: float, kind: str) -> float:
    """Apply the fixed conservative step and round to a readable value."""
    raw = current * LOOSEN_FACTOR
    if kind == "int":
        return float(math.ceil(raw))
    if kind == "seconds":
        # Nearest minute reads better than 450.0s.
        return float(round(raw / 60) * 60)
    # usd
    return round(raw, 2)


def _confidence_for(n: int) -> str:
    """Sample-size → confidence tier. Below MIN_SAMPLES it's not called."""
    if n >= 30:
        return "high"
    if n >= 10:
        return "medium"
    return "low"


def _pct(rate: float) -> str:
    return f"{rate * 100:.0f}%"


def _calibrate_symptom(
    symptom: SymptomType, labels: list[FeedbackLabel], policy: Policy
) -> SymptomCalibration:
    counts = Counter(labels)
    total = len(labels)
    good = counts[FeedbackLabel.GOOD_KILL]
    false_pos = counts[FeedbackLabel.FALSE_POSITIVE]
    missed = counts[FeedbackLabel.MISSED_KILL]
    other = counts[FeedbackLabel.OTHER]
    fp_rate = false_pos / total if total else 0.0

    knob = _SYMPTOM_KNOB.get(symptom)
    base = SymptomCalibration(
        symptom=symptom,
        total_labeled=total,
        good_kills=good,
        false_positives=false_pos,
        missed_kills=missed,
        other=other,
        false_positive_rate=fp_rate,
        confidence="insufficient_data",
        rationale="",
    )

    # 1. Not enough data to say anything.
    if total < MIN_SAMPLES_PER_SYMPTOM:
        return base.model_copy(
            update={
                "rationale": (
                    f"n={total} labeled kill(s); need "
                    f"{MIN_SAMPLES_PER_SYMPTOM}+ before suggesting a change."
                )
            }
        )

    confidence = _confidence_for(total)

    # 2. No single numeric knob for this symptom — stats only.
    if knob is None:
        return base.model_copy(
            update={
                "confidence": confidence,
                "rationale": (
                    f"{_pct(fp_rate)} false-positive (n={total}). No single "
                    f"numeric threshold maps to {symptom.value}; review the "
                    f"tool allowlist / liveness settings by hand."
                ),
            }
        )

    current = float(getattr(policy.thresholds, knob.field))

    # 3. Well-calibrated — false-positive rate within tolerance.
    if fp_rate < FALSE_POSITIVE_ACTION_THRESHOLD:
        return base.model_copy(
            update={
                "threshold_field": knob.field,
                "current_value": current,
                "confidence": confidence,
                "rationale": (
                    f"{_pct(fp_rate)} false-positive (n={total}) — within "
                    f"tolerance ({_pct(FALSE_POSITIVE_ACTION_THRESHOLD)}); "
                    f"no change suggested."
                ),
            }
        )

    # 4. Too many false positives — suggest loosening.
    suggested = _loosen(current, knob.kind)
    return base.model_copy(
        update={
            "threshold_field": knob.field,
            "current_value": current,
            "suggested_value": suggested,
            "confidence": confidence,
            "rationale": (
                f"{_pct(fp_rate)} of {symptom.value} kills under "
                f"'{policy.name}' were labeled false-positive (n={total}). "
                f"Consider raising {knob.field} "
                f"{_fmt(current, knob.kind)}→{_fmt(suggested, knob.kind)}."
            ),
        }
    )


def _fmt(value: float, kind: str) -> str:
    """Render a threshold value the way a human writes it in the policy."""
    if kind == "usd":
        return f"${value:g}"
    if kind == "seconds":
        return f"{value:g}s"
    return f"{value:g}"


def build_calibration_report(
    policy: Policy, labeled_kills: Iterable[LabeledKill]
) -> CalibrationReport:
    """Aggregate labeled kills into an advisory calibration report.

    Pure and deterministic: same inputs → same report, no I/O. Symptoms are
    reported in `SymptomType` declaration order, but only those with at least
    one labeled kill appear. A symptom with a high false-positive rate on a
    sufficient sample gets a loosening suggestion; everything else is
    stats-only (see the four branches in `_calibrate_symptom`).
    """
    by_symptom: dict[SymptomType, list[FeedbackLabel]] = {}
    for kill in labeled_kills:
        by_symptom.setdefault(kill.symptom, []).append(kill.label)

    symptoms = [
        _calibrate_symptom(symptom, by_symptom[symptom], policy)
        for symptom in SymptomType
        if symptom in by_symptom
    ]
    total = sum(s.total_labeled for s in symptoms)
    return CalibrationReport(
        policy_name=policy.name,
        total_labeled_kills=total,
        symptoms=symptoms,
    )
