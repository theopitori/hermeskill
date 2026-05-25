"""The `watch()` entrypoint — the 5-line customer integration.

Public API:

    from caspase import watch

    async def main():
        graph = await watch(my_graph, name="coding-bot-v1", policy="coding-default")
        await graph.ainvoke({"task": "fix the bug"})

What `watch()` does, in order:

1. Loads SDK config (.env / env vars) and constructs a `CaspaseClient`.
2. Calls `POST /agents` on the control plane to register, getting back an
   `agent_id` and the resolved policy.
3. Creates a `WatcherState`, registers it in the process-wide registry, and
   ensures the singleton `BackgroundWorker` is running.
4. Creates a `CaspaseCallbackHandler` bound to the new state.
5. Returns `graph.with_config({"callbacks": [handler]})` — the customer's
   own graph object, supervised.

The returned graph is a thin LangChain `Runnable` wrapper. The customer's
existing `.ainvoke()` / `.astream()` calls work unchanged.

Lifecycle: each `watch()` call leaves a `WatcherState` in the registry and
the worker running until the process exits. M2 will add an explicit
`unwatch(agent_id)` or context-manager form for short-lived agents.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from caspase.apoptosis import Watchdog, build_kill_event_payload
from caspase.client import CaspaseClient
from caspase.config import SDKConfig
from caspase.exceptions import CaspaseTerminated
from caspase.langchain import CaspaseCallbackHandler
from caspase.langgraph import with_caspase
from caspase.policies import resolve_policy
from caspase.watcher import (
    WatcherState,
    ensure_worker_started,
    register_watcher,
)

if TYPE_CHECKING:
    from langchain_core.runnables import Runnable

logger = logging.getLogger("caspase.watch")


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

    `client` is exposed mainly for tests — production callers should let
    `watch()` build one from `SDKConfig`.

    Returns the same graph re-configured with the Caspase callback handler.
    """
    # Resolve the policy *before* hitting the network — gives a clean
    # UnknownPolicyError at the watch() call site rather than a 4xx from
    # the server (which currently accepts any name; M5 tightens that up).
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
    # Attach the L2 watchdog. It doesn't start a thread until first
    # arm() (from on_chain_start), so creating it here is cheap.
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
    # Wrap so we can catch CaspaseTerminated from `ainvoke` and post the
    # death certificate as the closing act of the apoptosis sequence.
    return _WatchedRunnable(inner, state=state, client=client)


class _WatchedRunnable:
    """Thin wrapper around the LangChain Runnable returned by `with_caspase`.

    The inner runnable is the customer's graph with the Caspase callback
    handler attached — all the supervision happens there. This wrapper
    adds ONE thing: catching `CaspaseTerminated` from `ainvoke` and
    posting the death certificate to the control plane before re-raising.

    **The cert post is best-effort.** A 5xx, network drop, or server
    going away must NOT swallow the `CaspaseTerminated` the customer's
    code needs to see. Errors are logged ("forensic loss, not
    containment loss") and the original exception propagates unchanged.

    The 409 path — another kill_event already in flight for this agent
    (symptom-kill racing manual-kill, M4) — is treated as a success:
    someone else already filed the cert, our job here is done.

    Delegation: any attribute access not explicitly overridden falls
    through to the inner runnable via `__getattr__`. That keeps
    `wrapped.with_config(...)`, `wrapped.batch(...)`, `wrapped.stream(...)`
    etc. working unchanged.
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

    async def ainvoke(
        self,
        input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            return await self._inner.ainvoke(input, config, **kwargs)
        except CaspaseTerminated:
            await self._post_death_cert_best_effort()
            raise

    async def astream(
        self,
        input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Same wrapping for astream — yields from inner, posts cert if it
        raises CaspaseTerminated during streaming."""
        try:
            async for chunk in self._inner.astream(input, config, **kwargs):
                yield chunk
        except CaspaseTerminated:
            await self._post_death_cert_best_effort()
            raise

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attributes NOT found on self —
        # `ainvoke` and `astream` above stay overridden.
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
            result = await self._client.post_kill_event(
                self._state.agent_id, payload
            )
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
        # `result` is KillEventOut on 201, int on 409. Either way it
        # carries an id we can log for ops correlation.
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
