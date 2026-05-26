"""Caspase SDK — apoptosis protocol core for AI agent supervision.

Framework-agnostic core: WatcherState, symptom checks, death certificates,
kill-event client, operator CLI. Install a framework adapter on top:

    pip install caspase-hermes         # Hermes Agent plugin (recommended)
    pip install caspase[langgraph]     # legacy LangGraph adapter

The bare `caspase` package imports with no third-party agent-framework
dependencies; framework-specific code (LangGraph callback handler,
LangChain runnable wrapping) is lazy-loaded only when `watch()` is called
on a LangGraph object.

Public exceptions:

    from caspase import CaspaseTerminated

    # Raised by framework adapters when the agent is killed by Caspase.
    # Catch at your top-level run loop if you need cleanup before exit.

`checkpoint()` is a cooperative termination point for custom loops;
raises CaspaseTerminated if a kill directive is pending.
"""

from caspase._version import __version__
from caspase._watch import watch
from caspase.exceptions import CaspaseError, CaspaseTerminated

__all__ = [
    "CaspaseError",
    "CaspaseTerminated",
    "__version__",
    "checkpoint",
    "watch",
]


def checkpoint() -> None:
    """Cooperative termination point for non-LangGraph custom loops. Implemented in M2."""
    raise NotImplementedError("checkpoint() lands in M2")
