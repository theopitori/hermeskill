"""Stasis SDK — apoptosis protocol core for AI agent supervision.

Framework-agnostic core: WatcherState, symptom checks, death certificates,
kill-event client, operator CLI. Install a framework adapter on top:

    pip install stasis-hermes         # Hermes Agent plugin
    # stasis-agent[langgraph]         # LangGraph adapter (coming soon)

Public exceptions:

    from stasis_agent import StasisTerminated

    # Raised by framework adapters when the agent is killed by Stasis.
    # Catch at your top-level run loop if you need cleanup before exit.

`checkpoint()` is a cooperative termination point for custom loops;
raises StasisTerminated if a kill directive is pending.
"""

from stasis_agent._version import __version__
from stasis_agent._watch import watch
from stasis_agent.exceptions import StasisError, StasisTerminated

__all__ = [
    "StasisError",
    "StasisTerminated",
    "__version__",
    "checkpoint",
    "watch",
]


def checkpoint() -> None:
    """Cooperative termination point for non-LangGraph custom loops. Implemented in M2."""
    raise NotImplementedError("checkpoint() lands in M2")
