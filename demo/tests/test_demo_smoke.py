"""Smoke test for the flagship `python -m demo` showpiece.

Runs each scenario end-to-end against the real in-process control plane and
asserts a kill actually fired and a death certificate was filed. This is what
keeps the README GIF honest — if the demo ever stops killing, CI goes red.

`asyncio_mode = "auto"` (see root pyproject) means these are picked up without
an explicit marker. Scenarios run sequentially because each boots a uvicorn on
the fixed demo port.
"""

from __future__ import annotations

import pytest

from demo.__main__ import run_offline_demo
from demo.rogue import SCENARIOS

# Each scenario must terminate on this specific symptom.
_EXPECTED_SYMPTOM = {
    "loop": "loop",
    "cost": "token_runaway",
    "scope": "tool_scope_violation",
    "wall_clock": "wall_clock",
}


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_scenario_kills_and_files_certificate(scenario: str) -> None:
    outcome = await run_offline_demo(scenario, quiet=True)

    # The agent was actually killed by the real engine.
    assert outcome.result.killed, f"{scenario} produced no terminal verdict"
    terminal = outcome.result.terminal
    assert terminal is not None
    assert terminal.symptom.value == _EXPECTED_SYMPTOM[scenario]

    # A death certificate was filed with the control plane.
    assert outcome.kill_event_id is not None
    assert outcome.kill_event_id != -1

    # The kill is reflected in the watcher state the cert is built from.
    assert outcome.result.state.terminate_requested is True
    assert outcome.result.state.symptoms_log, "symptom was not recorded"


async def test_all_scenarios_are_covered() -> None:
    """Guard: every shipped scenario has an expected-symptom assertion."""
    assert set(SCENARIOS) == set(_EXPECTED_SYMPTOM)


async def test_hardkill_supervisor_kills_wedged_child_and_files_cert() -> None:
    """The L3 hardkill scenario spawns + force-kills a real wedged process.

    On POSIX (incl. CI) this exercises the SIGKILL escalation path; on Windows
    terminate() is already a hard kill. Either way the child must die and a
    death certificate must be filed.
    """
    from demo.hardkill import run_hardkill_demo

    outcome = await run_hardkill_demo(quiet=True)
    assert outcome.killed is True
    assert outcome.trigger == "heartbeat_loss"
    assert outcome.kill_event_id is not None
    assert outcome.kill_event_id != -1


async def test_calibrate_scenario_labels_kills_and_surfaces_suggestion() -> None:
    """The Phase-4 calibrate scenario files labelled kills and tunes a hint.

    Exercises the full feedback loop on SQLite end-to-end: register N agents,
    drive a real loop-kill each, submit operator labels through the *real*
    public ``POST /feedback/{token}`` endpoint, then fetch the calibration
    report. Asserts every verdict landed and the advisory suggestion appears.
    This is the guard for the SQLite-portable feedback path (naive-vs-aware
    ``expires_at`` comparison) that no Postgres test exercises.
    """
    from demo.calibrate import run_calibrate_demo

    outcome = await run_calibrate_demo(quiet=True)
    # Every operator verdict was recorded through the real feedback endpoint.
    assert outcome.labeled == 5, "not all feedback labels were accepted"
    # 60% of loop-kills were labelled false-positive → advisory loosening of
    # the strict loop cap (3 → ceil(3 * 1.5) = 5).
    assert outcome.loop_suggested_value == 5.0
