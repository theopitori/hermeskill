"""Hermeskill SDK — apoptosis protocol core for AI agent supervision.

Framework-agnostic core: WatcherState, symptom checks, death certificates,
kill-event client, operator CLI. Install a framework adapter on top:

    pip install hermeskill-hermes         # Hermes Agent plugin (recommended)

The bare `hermeskill` package imports with no third-party agent-framework
dependencies.

Public exceptions:

    from hermeskill import HermeskillTerminated

    # Raised by framework adapters and `checkpoint()` when the agent is
    # killed by Hermeskill. Catch at your top-level run loop if you need
    # cleanup before exit.

`checkpoint()` is a cooperative termination point for custom run loops;
raises HermeskillTerminated if a kill directive is pending.
"""

from hermeskill._version import __version__
from hermeskill.calibration import LabeledKill, build_calibration_report
from hermeskill.exceptions import HermeskillError, HermeskillTerminated
from hermeskill.supervisor import Heartbeat, ProcessSupervisor, SupervisorResult

__all__ = [
    "Heartbeat",
    "HermeskillError",
    "HermeskillTerminated",
    "LabeledKill",
    "ProcessSupervisor",
    "SupervisorResult",
    "__version__",
    "build_calibration_report",
    "checkpoint",
]


def checkpoint() -> None:
    """Cooperative termination point for custom run loops.

    Call inside long-running synchronous work to give Hermeskill a chance to
    terminate the agent. Raises HermeskillTerminated if any registered watcher
    has its apoptosis flag set; no-op otherwise. Safe to call from code with
    no registered watcher (returns immediately).
    """
    from hermeskill.exceptions import HermeskillTerminated
    from hermeskill.watcher import all_watchers

    for state in all_watchers():
        if state.terminate_requested:
            raise HermeskillTerminated(
                state.terminate_reason or "terminated",
                kill_event_id=state.terminate_kill_event_id,
            )
