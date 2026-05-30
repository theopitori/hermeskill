"""Caspase SDK — apoptosis protocol core for AI agent supervision.

Framework-agnostic core: WatcherState, symptom checks, death certificates,
kill-event client, operator CLI. Install a framework adapter on top:

    pip install caspase-hermes         # Hermes Agent plugin (recommended)

The bare `caspase` package imports with no third-party agent-framework
dependencies.

Public exceptions:

    from caspase import CaspaseTerminated

    # Raised by framework adapters and `checkpoint()` when the agent is
    # killed by Caspase. Catch at your top-level run loop if you need
    # cleanup before exit.

`checkpoint()` is a cooperative termination point for custom run loops;
raises CaspaseTerminated if a kill directive is pending.
"""

from caspase._version import __version__
from caspase.calibration import LabeledKill, build_calibration_report
from caspase.exceptions import CaspaseError, CaspaseTerminated
from caspase.supervisor import Heartbeat, ProcessSupervisor, SupervisorResult

__all__ = [
    "CaspaseError",
    "CaspaseTerminated",
    "Heartbeat",
    "LabeledKill",
    "ProcessSupervisor",
    "SupervisorResult",
    "__version__",
    "build_calibration_report",
    "checkpoint",
]


def checkpoint() -> None:
    """Cooperative termination point for custom run loops.

    Call inside long-running synchronous work to give Caspase a chance to
    terminate the agent. Raises CaspaseTerminated if any registered watcher
    has its apoptosis flag set; no-op otherwise. Safe to call from code with
    no registered watcher (returns immediately).
    """
    from caspase.exceptions import CaspaseTerminated
    from caspase.watcher import all_watchers

    for state in all_watchers():
        if state.terminate_requested:
            raise CaspaseTerminated(
                state.terminate_reason or "terminated",
                kill_event_id=state.terminate_kill_event_id,
            )
