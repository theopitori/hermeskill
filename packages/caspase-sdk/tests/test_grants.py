"""SDK-side M5 tests — grant application + client + cert presence.

Covers:
  * `apply_grants` pure function (demote vs pass-through, multiple grants,
    grant on one symptom doesn't suppress another).
  * `_apply_results` integration: a covered Terminal does not flip the
    apoptosis flag, but is still recorded as a Warning symptom event.
  * Manual kill bypasses grants (regression on the M4 contract).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from caspase.apoptosis import build_death_certificate
from caspase.checks import Terminal, Warning, apply_grants
from caspase.policies import resolve_policy
from caspase.types import SymptomType, TriggerType
from caspase.watcher import WatcherState


def _apply_results(state: WatcherState, verdicts: list[Terminal | Warning]) -> None:
    """Test helper: apply grants, record symptoms, flip apoptosis flag on Terminal.

    Replicates the grant-application + state-mutation logic that lives in each
    framework adapter's hook bridge (e.g. caspase_hermes.bridge.on_pre_tool_call).
    Lives here rather than in caspase.checks to keep the SDK framework-agnostic.
    """
    applied = apply_grants(verdicts, state.grants)
    for v in applied:
        severity = "terminal" if isinstance(v, Terminal) else "warning"
        state.record_symptom(symptom=v.symptom, severity=severity, reason=v.reason, detail=v.detail)
        if isinstance(v, Terminal) and not state.terminate_requested:
            state.request_termination(v.reason)


def _state() -> WatcherState:
    return WatcherState(
        agent_id=uuid4(), name="t", policy=resolve_policy("coding-default")
    )


def _grant(symptom: str, grant_id: str = "g1") -> dict[str, Any]:
    return {
        "id": grant_id,
        "symptoms": [symptom],
        "expires_at": "2099-01-01T00:00:00+00:00",
        "reason": "test",
    }


# --- apply_grants (pure) -------------------------------------------------


def test_apply_grants_no_grants_passes_through() -> None:
    results: list[Terminal | Warning] = [
        Terminal(symptom=SymptomType.LOOP, reason="r"),
    ]
    out = apply_grants(results, [])
    assert out == results  # same objects, same order


def test_apply_grants_demotes_covered_terminal() -> None:
    results: list[Terminal | Warning] = [
        Terminal(symptom=SymptomType.LOOP, reason="repeated foo"),
    ]
    out = apply_grants(results, [_grant("loop", grant_id="abc")])
    assert len(out) == 1
    assert isinstance(out[0], Warning)
    assert out[0].symptom == SymptomType.LOOP
    assert out[0].detail["grant_id"] == "abc"
    assert "suppressed by grant" in out[0].reason


def test_apply_grants_leaves_uncovered_terminal_alone() -> None:
    """Grant covering `loop` does not suppress `token_runaway`."""
    results: list[Terminal | Warning] = [
        Terminal(symptom=SymptomType.TOKEN_RUNAWAY, reason="cost"),
    ]
    out = apply_grants(results, [_grant("loop")])
    assert isinstance(out[0], Terminal)


def test_apply_grants_passes_through_existing_warnings() -> None:
    results: list[Terminal | Warning] = [
        Warning(symptom=SymptomType.LOOP, reason="already a warning"),
    ]
    out = apply_grants(results, [_grant("loop")])
    assert out == results


def test_apply_grants_multiple_grants_union() -> None:
    """Two grants together cover both symptoms; both Terminals demoted."""
    results: list[Terminal | Warning] = [
        Terminal(symptom=SymptomType.LOOP, reason="r1"),
        Terminal(symptom=SymptomType.TOOL_SCOPE_VIOLATION, reason="r2"),
    ]
    out = apply_grants(
        results,
        [_grant("loop", grant_id="g1"), _grant("tool_scope_violation", grant_id="g2")],
    )
    assert all(isinstance(o, Warning) for o in out)
    assert out[0].detail["grant_id"] == "g1"
    assert out[1].detail["grant_id"] == "g2"


def test_apply_grants_first_matching_grant_wins() -> None:
    """When two grants cover the same symptom, the first one's id is
    stamped. Order is by the grants list as provided by the server
    (newest-first in the heartbeat enrichment)."""
    results: list[Terminal | Warning] = [
        Terminal(symptom=SymptomType.LOOP, reason="r"),
    ]
    out = apply_grants(
        results,
        [_grant("loop", grant_id="first"), _grant("loop", grant_id="second")],
    )
    assert isinstance(out[0], Warning)
    assert out[0].detail["grant_id"] == "first"


# --- _apply_results integration ------------------------------------------


def test_apply_results_does_not_flip_flag_when_grant_covers() -> None:
    """A loop Terminal + a loop grant: the apoptosis flag stays False
    and the symptoms_log carries a Warning, not a Terminal."""
    s = _state()
    s.grants = [_grant("loop", grant_id="g-loop")]

    _apply_results(s, [Terminal(symptom=SymptomType.LOOP, reason="loop fired")])

    assert s.terminate_requested is False, "grant should have suppressed the kill"
    # Symptom event recorded as a warning, with grant_id in detail.
    assert len(s.symptoms_log) == 1
    entry = s.symptoms_log[0]
    assert entry["severity"] == "warning"
    assert entry["detail"]["grant_id"] == "g-loop"


def test_apply_results_flips_flag_when_no_grant_covers() -> None:
    """Regression: no grant in cache → Terminal still flips the flag
    (M2 behavior unchanged)."""
    s = _state()
    s.grants = []
    _apply_results(s, [Terminal(symptom=SymptomType.LOOP, reason="loop fired")])
    assert s.terminate_requested is True
    assert s.terminate_reason == "loop fired"


def test_apply_results_partial_grant_still_kills_on_uncovered() -> None:
    """Grant covers `loop` but `token_runaway` fires — agent still dies
    and the cert reason is `token_runaway` (first-cause-wins on the
    uncovered Terminal)."""
    s = _state()
    s.grants = [_grant("loop")]
    _apply_results(
        s,
        [
            Terminal(symptom=SymptomType.LOOP, reason="loop fired"),
            Terminal(symptom=SymptomType.TOKEN_RUNAWAY, reason="cost cap"),
        ],
    )
    assert s.terminate_requested is True
    assert "cost cap" in (s.terminate_reason or "")


# --- manual kill bypass (M4 invariant) ----------------------------------


def test_manual_kill_bypasses_grant() -> None:
    """Operator-issued kill goes through `request_termination()` directly,
    not `_apply_results` — so an active grant must not save the agent.
    """
    s = _state()
    s.grants = [_grant("loop"), _grant("tool_scope_violation")]
    s.request_termination(
        "manual kill: deploy rollback",
        manual_kill={
            "operator": "op-key",
            "operator_reason": "deploy rollback",
            "kill_event_id": 7,
        },
    )
    assert s.terminate_requested is True
    cert = build_death_certificate(s)
    assert cert.trigger_type == TriggerType.MANUAL
    assert cert.operator_reason == "deploy rollback"


# --- cert sanity: warnings show up in the cert ---------------------------


def test_cert_includes_suppressed_warnings() -> None:
    """A grant-suppressed symptom should still appear in the death cert's
    symptoms_log so operators can audit 'what would have killed this
    agent?' post-mortem."""
    s = _state()
    s.grants = [_grant("loop", grant_id="g-audit")]
    # Trigger the suppressed warning, then a different Terminal that
    # actually kills.
    _apply_results(s, [Terminal(symptom=SymptomType.LOOP, reason="loop fired")])
    _apply_results(
        s,
        [Terminal(symptom=SymptomType.TOKEN_RUNAWAY, reason="cost cap")],
    )
    cert = build_death_certificate(s)
    symptoms = {entry["symptom"]: entry for entry in cert.symptoms_log}
    assert "loop" in symptoms
    assert symptoms["loop"]["severity"] == "warning"
    assert symptoms["loop"]["detail"]["grant_id"] == "g-audit"
    assert "token_runaway" in symptoms
    assert symptoms["token_runaway"]["severity"] == "terminal"
