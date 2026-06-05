"""Tests for the L3 ProcessSupervisor (the honest hard kill).

Safety discipline (these spawn REAL ``while True`` children):
  * every scenario sets sub-second caps + a wall_clock backstop, so a logic
    bug can't hang the run;
  * ``ProcessSupervisor.run`` itself kills the child in a ``finally``;
  * graceful-SIGTERM assertions are POSIX-only — on Windows ``terminate()`` is
    already a hard kill, so there is no cooperative window to test.

Targets live in ``_supervisor_targets`` (a real importable module) because the
spawn start method re-imports the target's module in the child.
"""

from __future__ import annotations

import sys

import pytest
from _supervisor_targets import (
    cooperative_on_sigterm,
    returns_quickly,
    wedged_ignores_sigterm,
    wedged_no_heartbeat,
)
from hermeskill.supervisor import (
    TRIGGER_HEARTBEAT_LOSS,
    TRIGGER_WALL_CLOCK,
    ProcessSupervisor,
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows terminate() is already a hard kill — no catchable SIGTERM",
)


def _step_names(result: object) -> list[str]:
    return [s["step"] for s in result.shutdown_steps]  # type: ignore[attr-defined]


def test_clean_completion_is_not_killed() -> None:
    sup = ProcessSupervisor(
        grace_seconds=0.3,
        wall_clock_seconds=5.0,
        heartbeat_timeout_seconds=2.0,
    )
    result = sup.run(returns_quickly, use_heartbeat=True)
    assert result.completed_cleanly
    assert result.killed is False
    assert result.exit_code == 0


def test_heartbeat_loss_kills_wedged_child() -> None:
    sup = ProcessSupervisor(
        grace_seconds=0.3,
        wall_clock_seconds=3.0,  # backstop so the test can never hang
        heartbeat_timeout_seconds=0.3,
    )
    result = sup.run(wedged_no_heartbeat, use_heartbeat=True)
    assert result.trigger == TRIGGER_HEARTBEAT_LOSS
    assert result.killed is True
    assert "supervisor_sigterm" in _step_names(result)


def test_wall_clock_kills_wedged_child() -> None:
    sup = ProcessSupervisor(grace_seconds=0.3, wall_clock_seconds=0.3)
    result = sup.run(wedged_no_heartbeat, use_heartbeat=True)
    assert result.trigger == TRIGGER_WALL_CLOCK
    assert result.killed is True


@posix_only
def test_sigterm_ignored_requires_sigkill() -> None:
    sup = ProcessSupervisor(
        grace_seconds=0.4,
        wall_clock_seconds=3.0,
        heartbeat_timeout_seconds=0.3,
    )
    result = sup.run(wedged_ignores_sigterm, use_heartbeat=True)
    assert result.trigger == TRIGGER_HEARTBEAT_LOSS
    assert result.killed is True
    assert result.sigkilled is True
    assert "supervisor_sigkill" in _step_names(result)


@posix_only
def test_cooperative_exit_skips_sigkill() -> None:
    sup = ProcessSupervisor(
        grace_seconds=1.5,
        wall_clock_seconds=4.0,
        heartbeat_timeout_seconds=0.3,
    )
    result = sup.run(cooperative_on_sigterm, use_heartbeat=True)
    assert result.killed is True
    assert result.sigkilled is False
    assert "supervisor_exited_after_sigterm" in _step_names(result)
    assert "supervisor_sigkill" not in _step_names(result)


def test_picklability_guard_rejects_local_function() -> None:
    sup = ProcessSupervisor(grace_seconds=0.3, wall_clock_seconds=1.0)

    def local_target() -> None:  # not module-level → unpicklable under spawn
        pass

    with pytest.raises(ValueError, match="picklable"):
        sup.run(local_target)


def test_record_step_callback_receives_steps() -> None:
    seen: list[str] = []
    sup = ProcessSupervisor(
        grace_seconds=0.3,
        wall_clock_seconds=0.3,
        record_step=lambda step, detail: seen.append(step),
    )
    sup.run(wedged_no_heartbeat, use_heartbeat=True)
    assert "supervisor_sigterm" in seen
