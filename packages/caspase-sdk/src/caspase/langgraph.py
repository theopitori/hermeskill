"""LangGraph integration: attaches the Caspase callback to a compiled graph.

The plan originally talked about injecting `_caspase_gate` check nodes between
every node boundary. We don't actually need that — LangGraph fires
`on_chain_start` per node, and our `CaspaseCallbackHandler` raises
`CaspaseTerminated` from inside that callback when the apoptosis flag is set.
Same effect, but stays on LangChain's public callback API (not LangGraph's
internal node structure).

This module just provides `with_caspase(graph, handler)` — a thin wrapper that
attaches the handler via `with_config`. We isolate it here so that if
LangGraph's config API changes, only this file moves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caspase.langchain import CaspaseCallbackHandler

if TYPE_CHECKING:
    from langchain_core.runnables import Runnable


def with_caspase(graph: Any, handler: CaspaseCallbackHandler) -> Runnable[Any, Any]:
    """Attach the Caspase callback handler to a graph (or any Runnable).

    Works for both compiled LangGraph state graphs and bare LangChain
    Runnables. If the graph hasn't been compiled yet, `with_config` will fail
    cleanly — the caller is expected to pass a compiled graph.
    """
    if not hasattr(graph, "with_config"):
        raise TypeError(
            f"with_caspase() expects a compiled LangGraph or LangChain Runnable; "
            f"got {type(graph).__name__}. Did you forget to .compile() the graph?"
        )
    result: Runnable[Any, Any] = graph.with_config({"callbacks": [handler]})
    return result
