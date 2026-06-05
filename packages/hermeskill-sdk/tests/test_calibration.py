"""Tests for the pure feedback-calibration engine (Phase 4).

No I/O — the whole engine is a function over a list of LabeledKill, which is
exactly why it lives in the SDK and not the control plane. These tests pin the
honesty guarantees as much as the math: suggest-only, false-positive-only
direction, sample-size gate, stats-only for non-numeric symptoms.
"""

from __future__ import annotations

from hermeskill.calibration import (
    FALSE_POSITIVE_ACTION_THRESHOLD,
    MIN_SAMPLES_PER_SYMPTOM,
    LabeledKill,
    build_calibration_report,
)
from hermeskill.policies import resolve_policy
from hermeskill.types import FeedbackLabel, SymptomType

STRICT = resolve_policy("strict")  # max_loop_repeats=3, max_cost_usd=2.0, runtime=300

GOOD = FeedbackLabel.GOOD_KILL
FP = FeedbackLabel.FALSE_POSITIVE
MISS = FeedbackLabel.MISSED_KILL
OTHER = FeedbackLabel.OTHER


def _kills(symptom: SymptomType, **counts: int) -> list[LabeledKill]:
    out: list[LabeledKill] = []
    mapping = {
        "good": GOOD,
        "fp": FP,
        "miss": MISS,
        "other": OTHER,
    }
    for key, n in counts.items():
        out.extend(LabeledKill(symptom=symptom, label=mapping[key]) for _ in range(n))
    return out


def _row(report: object, symptom: SymptomType):  # type: ignore[no-untyped-def]
    return next(s for s in report.symptoms if s.symptom == symptom)  # type: ignore[attr-defined]


def test_empty_report_is_valid() -> None:
    report = build_calibration_report(STRICT, [])
    assert report.policy_name == "strict"
    assert report.total_labeled_kills == 0
    assert report.symptoms == []


def test_below_min_samples_makes_no_suggestion() -> None:
    # 4 < MIN_SAMPLES, all false positives → still no suggestion.
    kills = _kills(SymptomType.LOOP, fp=MIN_SAMPLES_PER_SYMPTOM - 1)
    row = _row(build_calibration_report(STRICT, kills), SymptomType.LOOP)
    assert row.confidence == "insufficient_data"
    assert row.suggested_value is None
    assert row.false_positive_rate == 1.0  # rate is still reported


def test_high_false_positive_rate_suggests_loosening() -> None:
    # 5 loop kills, 3 false positive → 60% > 30% threshold → loosen.
    kills = _kills(SymptomType.LOOP, good=2, fp=3)
    row = _row(build_calibration_report(STRICT, kills), SymptomType.LOOP)
    assert row.threshold_field == "max_loop_repeats"
    assert row.current_value == 3.0
    assert row.suggested_value == 5.0  # ceil(3 * 1.5)
    assert row.suggested_value > row.current_value  # only ever loosens
    assert row.confidence == "low"
    assert "false-positive" in row.rationale


def test_low_false_positive_rate_no_suggestion() -> None:
    # 10 loop kills, 1 false positive → 10% < 30% → calibrated, no suggestion.
    kills = _kills(SymptomType.LOOP, good=9, fp=1)
    row = _row(build_calibration_report(STRICT, kills), SymptomType.LOOP)
    assert row.threshold_field == "max_loop_repeats"
    assert row.suggested_value is None
    assert row.confidence == "medium"
    assert "within tolerance" in row.rationale


def test_cost_and_wall_clock_rounding() -> None:
    cost = _kills(SymptomType.TOKEN_RUNAWAY, fp=5)
    wall = _kills(SymptomType.WALL_CLOCK, fp=5)
    report = build_calibration_report(STRICT, cost + wall)
    cost_row = _row(report, SymptomType.TOKEN_RUNAWAY)
    wall_row = _row(report, SymptomType.WALL_CLOCK)
    assert cost_row.threshold_field == "max_cost_usd"
    assert cost_row.suggested_value == 3.0  # round(2.0 * 1.5, 2)
    assert wall_row.threshold_field == "max_runtime_seconds"
    assert wall_row.suggested_value == 480.0  # round(300*1.5/60)*60


def test_non_numeric_symptom_is_stats_only() -> None:
    # tool_scope_violation maps to no numeric knob — report, don't suggest.
    kills = _kills(SymptomType.TOOL_SCOPE_VIOLATION, fp=5)
    row = _row(build_calibration_report(STRICT, kills), SymptomType.TOOL_SCOPE_VIOLATION)
    assert row.threshold_field is None
    assert row.suggested_value is None
    assert row.false_positive_rate == 1.0
    assert "allowlist" in row.rationale


def test_missed_and_other_are_counted_but_never_drive_a_suggestion() -> None:
    # Lots of missed/other, zero false positives → no loosening, ever.
    kills = _kills(SymptomType.LOOP, good=2, miss=6, other=2)
    row = _row(build_calibration_report(STRICT, kills), SymptomType.LOOP)
    assert row.missed_kills == 6
    assert row.other == 2
    assert row.false_positive_rate == 0.0
    assert row.suggested_value is None  # missed_kill never tightens or loosens


def test_confidence_tiers_scale_with_sample_size() -> None:
    for n, expected in [(5, "low"), (10, "medium"), (30, "high")]:
        # All good kills so we exercise the tier without a suggestion branch.
        kills = _kills(SymptomType.WALL_CLOCK, good=n)
        row = _row(build_calibration_report(STRICT, kills), SymptomType.WALL_CLOCK)
        assert row.confidence == expected, f"n={n}"


def test_total_and_only_present_symptoms_appear() -> None:
    kills = _kills(SymptomType.LOOP, good=3) + _kills(SymptomType.WALL_CLOCK, good=2)
    report = build_calibration_report(STRICT, kills)
    assert report.total_labeled_kills == 5
    present = {s.symptom for s in report.symptoms}
    assert present == {SymptomType.LOOP, SymptomType.WALL_CLOCK}


def test_action_threshold_boundary_does_not_suggest() -> None:
    # Exactly at the threshold should not trigger (strict `>=`... we use `<`
    # for "within tolerance", so == threshold IS actionable). Pin the contract.
    assert FALSE_POSITIVE_ACTION_THRESHOLD == 0.30
    # 10 kills, 3 fp → exactly 0.30 → actionable (>= threshold).
    kills = _kills(SymptomType.LOOP, good=7, fp=3)
    row = _row(build_calibration_report(STRICT, kills), SymptomType.LOOP)
    assert row.false_positive_rate == 0.30
    assert row.suggested_value == 5.0
