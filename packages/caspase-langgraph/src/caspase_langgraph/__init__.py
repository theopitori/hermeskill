"""Caspase apoptosis supervision for LangGraph / LangChain agents.

Wrap a compiled graph (or any LangChain ``Runnable``) and Caspase watches it:
loop / cost / wall-clock / tool-scope checks run at every node and tool
boundary, and when a terminal symptom fires the agent is cooperatively
terminated (``CaspaseTerminated`` is raised at the next boundary) and a death
certificate is filed with the control plane.

Quickstart
----------

    pip install "caspase-langgraph[graph]"

    from caspase_langgraph import watch

    async def main():
        graph = await watch(my_compiled_graph, name="bot-v1", policy="coding-default")
        await graph.ainvoke({"task": "fix the failing test"})

Configuration (env vars or ``.env``)::

    CASPASE_API_KEY    — your operator API key (required)
    CASPASE_BASE_URL   — control plane URL (default: http://localhost:8000)

How it works
------------

``watch()`` registers the agent, starts the shared background worker
(heartbeats + event drain), attaches :class:`CaspaseCallbackHandler` to the
graph, and returns a thin wrapper. LangChain has no "block this call" return
channel like Hermes does, so the kill mechanism is to *raise*
``CaspaseTerminated`` from a callback (with ``raise_error = True`` so it
propagates). The wrapper catches that, posts the death certificate, and
re-raises so the customer's own ``try/finally`` cleanup runs.

This package depends only on ``langchain-core``; install the ``[graph]`` extra
to pull in ``langgraph`` itself for the ``StateGraph`` integration path. The
core ``caspase`` SDK has no LangChain dependency — adapters are thin and
opt-in, one package per runtime (see also ``caspase-hermes``).
"""

from __future__ import annotations

from caspase.exceptions import CaspaseError, CaspaseTerminated

from caspase_langgraph.adapter import watch, with_caspase
from caspase_langgraph.callback import CaspaseCallbackHandler

__version__ = "0.1.0a0"

__all__ = [
    "CaspaseCallbackHandler",
    "CaspaseError",
    "CaspaseTerminated",
    "watch",
    "with_caspase",
]
