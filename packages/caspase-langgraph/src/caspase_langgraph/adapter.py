"""The ``watch()`` entrypoint and the graph wrapper — the customer integration.

This is the LangGraph analog of ``caspase_hermes.plugin``: it owns the
control-plane lifecycle (register the agent, start the shared background
worker, post the death certificate) and wraps the customer's graph with the
:class:`~caspase_langgraph.callback.CaspaseCallbackHandler`.

Public API — five lines for the customer::

    from caspase_langgraph import watch

    async def main():
        graph = await watch(my_graph, name="coding-bot-v1", policy="coding-default")
        await graph.ainvoke({"task": "fix the bug"})

What ``watch()`` does, in order:

1. Resolves the policy (clean error at the call site, before any network).
2. ``POST /agents`` to register, getting an ``agent_id``.
3. Creates a ``WatcherState``, registers it in the process-wide registry, and
   ensures the singleton ``BackgroundWorker`` (heartbeats + event drain) runs.
4. Attaches a ``CaspaseCallbackHandler`` to the graph via ``with_config``.
5. Returns a thin wrapper whose ``ainvoke`` / ``astream`` post the death cert
   when apoptosis raises ``CaspaseTerminated``.

The customer's existing ``.ainvoke()`` / ``.astream()`` calls work unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from caspase.apoptosis import Watchdog, build_kill_event_payload
from caspase.client import CaspaseClient
from caspase.config import SDKConfig
from caspase.exceptions import CaspaseTerminated
from caspase.policies import resolve_policy
from caspase.watcher import (
    WatcherState,
    ensure_worker_started,
    register_watcher,
)

from caspase_langgraph.callback import CaspaseCallbackHandler

if TYPE_CHECKING:
    from langchain_core.runnables import Runnable

logger = logging.getLogger("caspase_langgraph.adapter")


def with_caspase(graph: Any, handler: CaspaseCallbackHandler) -> Runnable[Any, Any]:
    """Attach the Caspase callback handler to a graph (or any Runnable).

    Works for both compiled LangGraph state graphs and bare LangChain
    Runnables. Isolated here so that if LangChain's config API changes, only
    this function moves. Raises ``TypeError`` if handed something that isn't a
    Runnable (e.g. an uncompiled ``StateGraph``).
    """
    if not hasattr(graph, "with_config"):
        raise TypeError(
            f"with_caspase() expects a compiled LangGraph or LangChain Runnable; "
            f"got {type(graph).__name__}. Did you forget to .compile() the graph?"
        )
    result: Runnable[Any, Any] = graph.with_config({"callbacks": [handler]})
    return result


async def watch(
    graph: Any,
    *,
    name: str,
    policy: str,
    metadata: dict[str, Any] | None = None,
    config: SDKConfig | None = None,
    client: CaspaseClient | None = None,
) -> _WatchedRunnable:
    """Wrap a LangGraph (or any LangChain Runnable) for Caspase supervision.

    ``client`` is exposed mainly for tests — production callers let ``watch()``
    build one from ``SDKConfig``. Returns the customer's graph re-configured
    with the Caspase callback handler, wrapped so apoptosis posts a death cert.
    """
    # Resolve the policy *before* hitting the network — a clean
    # UnknownPolicyError at the call site beats a 4xx from the server.
    resolved_policy = resolve_policy(policy)

    owns_client = client is None
    if client is None:
        client = CaspaseClient.from_config(config)

    try:
        registration = await client.register_agent(
            name=name,
            policy_name=resolved_policy.name,
            metadata=metadata or {},
        )
    except Exception:
        if owns_client:
            await client.aclose()
        raise

    state = WatcherState(
        agent_id=registration.agent_id,
        name=name,
        policy=resolved_policy,
    )
    # Attach the L2 watchdog. It doesn't start a thread until first arm()
    # (from on_chain_start), so creating it here is cheap.
    state.watchdog = Watchdog(
        state,
        grace_seconds=resolved_policy.thresholds.cooperative_grace_seconds,
    )
    register_watcher(state)
    ensure_worker_started(client)

    handler = CaspaseCallbackHandler(state)
    state.record_lifecycle("registered", agent_id=str(state.agent_id))

    logger.info(
        "caspase: watching agent %s (id=%s, policy=%s)",
        name,
        state.agent_id,
        resolved_policy.name,
    )

    inner = with_caspase(graph, handler)
    return _WatchedRunnable(inner, state=state, client=client)


class _WatchedRunnable:
    """Thin wrapper around the Runnable returned by :func:`with_caspase`.

    The inner runnable is the customer's graph with the Caspase callback
    attached — all the supervision happens there. This wrapper adds ONE thing:
    catching ``CaspaseTerminated`` from ``ainvoke`` / ``astream`` and posting
    the death certificate before re-raising.

    **The cert post is best-effort.** A 5xx, network drop, or server going away
    must NOT swallow the ``CaspaseTerminated`` the customer's code needs to see
    — errors are logged ("forensic loss, not containment loss") and the
    original exception propagates unchanged. A 409 (another kill_event already
    in flight for this agent) is treated as success: someone else filed it.

    Delegation: any attribute not overridden here falls through to the inner
    runnable via ``__getattr__``, so ``with_config`` / ``batch`` / ``stream``
    keep working unchanged.
    """

    def __init__(
        self,
        inner: Runnable[Any, Any],
        *,
        state: WatcherState,
        client: CaspaseClient,
    ) -> None:
        self._inner = inner
        self._state = state
        self._client = client

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        try:
            return await self._inner.ainvoke(input, config, **kwargs)
        except CaspaseTerminated:
            await self._post_death_cert_best_effort()
            raise

    async def astream(
        self, input: Any, config: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """Same wrapping for astream — yields from inner, posts the cert if it
        raises CaspaseTerminated mid-stream."""
        try:
            async for chunk in self._inner.astream(input, config, **kwargs):
                yield chunk
        except CaspaseTerminated:
            await self._post_death_cert_best_effort()
            raise

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attributes NOT found on self — ainvoke /
        # astream above stay overridden.
        return getattr(self._inner, name)

    async def _post_death_cert_best_effort(self) -> None:
        """Build + POST the cert; log on any failure, never raise."""
        t0 = time.monotonic()
        try:
            payload = build_kill_event_payload(self._state)
        except Exception:
            logger.exception(
                "caspase: failed to build death certificate for agent %s "
                "(forensic loss, not containment loss)",
                self._state.agent_id,
            )
            return
        try:
            result = await self._client.post_kill_event(self._state.agent_id, payload)
        except Exception:
            logger.exception(
                "caspase: failed to POST death certificate for agent %s "
                "(forensic loss, not containment loss)",
                self._state.agent_id,
            )
            self._state.record_shutdown_step(
                "death_cert_post_failed",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
            return
        duration_ms = (time.monotonic() - t0) * 1000
        # `result` is KillEventOut on 201, int on 409. Either carries an id.
        kill_event_id: int | str
        if isinstance(result, int):
            kill_event_id = result
            self._state.record_shutdown_step(
                "death_cert_post_skipped_409",
                duration_ms=duration_ms,
                existing_kill_event_id=result,
            )
        else:
            kill_event_id = result.id
            self._state.record_shutdown_step(
                "death_cert_posted",
                duration_ms=duration_ms,
                kill_event_id=result.id,
            )
        logger.info(
            "caspase: death certificate posted for agent %s (kill_event=%s)",
            self._state.agent_id,
            kill_event_id,
        )
