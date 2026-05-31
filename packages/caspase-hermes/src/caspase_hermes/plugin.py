"""Hermes plugin hook handlers for Caspase.

This module wires the Hermes hook API to the Caspase apoptosis engine. One
``CaspasePlugin`` instance is created per Hermes session by ``register()`` in
``__init__.py``.

Kill path (cooperative, via ``pre_tool_call`` block directive)
--------------------------------------------------------------

Hermes v0.14 hooks are non-blocking — the runtime catches ``Exception`` from
hook callbacks and logs it without crashing the agent (see
``hermes_cli/plugins.py::invoke_hook`` and the matching
``get_pre_tool_call_block_message`` consumer in ``agent/tool_executor.py``).

The canonical way for a plugin to refuse a tool call is to return a dict
from ``pre_tool_call``::

    {"action": "block", "message": "Reason the tool was blocked"}

Hermes wraps that message into a tool error response (``{"error": ...}``)
and feeds it to the LLM instead of running the tool. PR #26759 explicitly
describes this as the canonical interception path for "rate limiting,
security restrictions, approval workflows" — and apoptosis fits squarely
in that bucket.

Effect when Caspase fires:
  1. ``pre_tool_call`` notices ``state.terminate_requested`` is True
     (set earlier by a symptom check or the manual-kill poller)
  2. We return the block directive
  3. The agent reads "caspase: <reason>" as a tool error and the next
     LLM turn typically concludes the session ("I cannot continue;
     terminating") because every subsequent tool call also blocks
  4. When the agent's loop ends naturally, Hermes fires ``on_session_end``
     and we POST the death certificate

Why block-only and not ``ctx.register_tool(override=True)`` with SystemExit:
  - block-directive is the documented and tested Hermes path; tool_override
    in v0.14 means "swap a tool's implementation" (per PR #26759) and would
    require us to fabricate a schema for every potentially-called tool
  - cooperative semantics match our SDK's "L1 cooperative termination"
    contract: we stop further harm immediately (no tool execution after
    kill) but let the agent's natural loop wind down
  - no SystemExit-across-thread weirdness

Background worker lifecycle
---------------------------

``register()`` calls ``caspase.watcher.ensure_worker_started(client)``.
This starts the shared per-process ``BackgroundWorker`` (heartbeats + event
drain) and the ``KillPendingPoller`` (manual-kill delivery). Both
singletons survive across Hermes sessions in the same process — safe
because they only reference the module-level ``_REGISTRY``.

On ``on_session_end``, the plugin calls ``BackgroundWorker.stop()`` to
flush remaining events before Hermes tears the session down.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import sys
import threading
import time
from collections.abc import Coroutine
from typing import Any
from uuid import UUID, uuid4

from caspase.apoptosis import (
    Watchdog,
    build_death_certificate,
    build_kill_event_payload,
)
from caspase.certificate import render_certificate, save_certificate
from caspase.client import CaspaseClient, TransportError
from caspase.policies import resolve_policy
from caspase.types import Policy
from caspase.watcher import (
    BackgroundWorker,
    KillPendingPoller,
    WatcherState,
    ensure_worker_started,
    register_watcher,
    unregister_watcher,
)

from caspase_hermes.bridge import (
    on_post_api_request,
    on_post_tool_call,
    on_pre_llm_call,
    on_pre_tool_call,
)
from caspase_hermes.bridge import (
    on_session_end as bridge_on_session_end,
)

logger = logging.getLogger("caspase_hermes.plugin")


class _SessionLoop:
    """A dedicated asyncio event loop on its own daemon thread, alive for the
    whole Hermes session.

    Hermes drives our hooks **synchronously** — ``register()`` and
    ``on_session_end`` are plain function calls, not awaited. But Caspase's I/O
    (agent registration, the heartbeat/event-drain worker, the kill poller, and
    the death-cert POST) is all ``async`` and shares one ``httpx.AsyncClient``.

    An ``httpx.AsyncClient`` binds to the event loop that first drives it and
    cannot be reused from another loop. The original design called
    ``asyncio.run()`` once in ``register()`` and again in ``on_session_end()``;
    that opened two *different* loops, each closed on return, so:

      * the ``BackgroundWorker``, created via ``loop.create_task`` on the first
        (immediately-closed) loop, never ticked — heartbeats and event drains
        silently never ran during the session; and
      * the death-cert POST on the second loop reused the client whose
        connection pool belonged to the first, now-closed loop, raising
        ``RuntimeError: Event loop is closed``.

    Running one ``run_forever`` loop on a background thread for the session's
    lifetime fixes both: the worker actually runs, and every async call —
    including teardown — happens on the one loop that owns the client.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="caspase-session-loop",
            daemon=True,
        )
        self._thread.start()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Schedule a coroutine on the session loop; return a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
        """Schedule a coroutine and block the calling thread until it completes."""
        return self.submit(coro).result(timeout)

    def close(self) -> None:
        """Stop the loop and join its thread. Idempotent and best-effort."""
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._thread.is_alive() and not self._loop.is_closed():
            with contextlib.suppress(Exception):
                self._loop.close()


class CaspasePlugin:
    """One per Hermes session. Owns the WatcherState lifecycle for that session."""

    def __init__(
        self,
        *,
        name: str,
        policy: str,
        metadata: dict[str, Any] | None = None,
        client: CaspaseClient,
        forced_offline: bool = False,
        local_cert: bool = True,
    ) -> None:
        self._client = client
        self._name = name
        self._policy_name = policy
        self._metadata = metadata or {}
        self._state: WatcherState | None = None
        self._loop_thread: _SessionLoop | None = None
        # forced_offline: no API key was configured, so skip every control-plane
        # call from the start (registration, worker, poller, death-cert POST) —
        # the client carries an empty key and must never hit the network.
        self._forced_offline = forced_offline
        # local_cert: render + save the death certificate locally on a kill.
        self._local_cert = local_cert

    def start(self) -> None:
        """Synchronous entry point used by Hermes' ``register()``.

        Spins up the session loop thread and runs :meth:`setup` on it, blocking
        the calling thread until registration completes (or fails). Safe to call
        from a thread with no running event loop (Hermes' case) or one running a
        *different* loop — the work happens on our own loop, never the caller's.
        """
        self._loop_thread = _SessionLoop()
        self._loop_thread.run(self.setup())

    async def astart(self) -> None:
        """Async entry point for callers already inside a running event loop.

        Identical to :meth:`start` but awaits setup via ``wrap_future`` so the
        caller's loop is never blocked.
        """
        self._loop_thread = _SessionLoop()
        await asyncio.wrap_future(self._loop_thread.submit(self.setup()))

    async def setup(self) -> None:
        """Register the agent with the control plane and wire up the watcher.

        Fail-open on connectivity. If the control plane is unreachable at
        registration time we DO NOT abort — a safety supervisor that fails
        to load is the worst outcome, because Hermes' loader would then run
        the agent with zero hooks and zero supervision, silently. Instead we
        mint a local agent_id, wire the watcher anyway, and mark the session
        offline. Local symptom checks (loop / token_runaway / wall_clock /
        tool_scope) run entirely in-process and need no control plane; only
        operator visibility, manual kill, grants, and death-cert archival are
        degraded until the control plane returns.
        """
        resolved_policy = resolve_policy(self._policy_name)

        agent_id, offline = await self._register_agent(resolved_policy)

        state = WatcherState(
            agent_id=agent_id,
            name=self._name,
            policy=resolved_policy,
        )
        state.offline = offline
        state.watchdog = Watchdog(
            state,
            grace_seconds=resolved_policy.thresholds.cooperative_grace_seconds,
        )
        register_watcher(state)
        # The background worker + kill poller only talk to the control plane
        # (heartbeats, event drain, manual-kill delivery). Offline they can
        # never succeed and would log a connection-refused traceback every
        # few seconds, so don't boot them. In-process symptom checks and the
        # L2 watchdog run independently — the kill path is unaffected.
        if not offline:
            ensure_worker_started(self._client)
        self._state = state

        state.record_lifecycle(
            "registered", agent_id=str(state.agent_id), offline=offline
        )
        if offline:
            logger.warning(
                "caspase: control plane unreachable; watching %r in LOCAL-ONLY "
                "mode (local id=%s, policy=%s) — symptom checks active, but "
                "operator visibility, manual kill, grants, and death-cert "
                "archival are unavailable until the control plane returns.",
                self._name,
                state.agent_id,
                resolved_policy.name,
            )
        else:
            logger.info(
                "caspase: watching %r (id=%s, policy=%s)",
                self._name,
                state.agent_id,
                resolved_policy.name,
            )

    async def _register_agent(self, policy: Policy) -> tuple[UUID, bool]:
        """Register with the control plane, falling back to local-only mode.

        Returns ``(agent_id, offline)``. On a transport failure (control
        plane down / unreachable) we mint a local UUID and return
        ``offline=True`` so :meth:`setup` can still wire all hooks. Other
        errors (auth, server 5xx) are NOT swallowed here — those signal a
        misconfiguration the operator must fix, not a transient outage.
        """
        # No API key configured → don't even attempt registration. Hitting a
        # reachable control plane with an empty key would 401 (AuthError),
        # which we deliberately DON'T swallow; forcing offline up front keeps
        # the keyless path clean.
        if self._forced_offline:
            return uuid4(), True
        try:
            registration = await self._client.register_agent(
                name=self._name,
                policy_name=policy.name,
                metadata=self._metadata,
            )
        except TransportError:
            return uuid4(), True
        return registration.agent_id, False

    # --- hook handlers -------------------------------------------------------

    def pre_tool_call(self, tool_name: str, args: Any) -> dict[str, str] | None:
        """Pre-tool checkpoint. Returns Hermes' block directive if kill is armed.

        Returning a dict with action="block" tells Hermes to refuse the tool
        call and surface the message as the tool's error result. Returning
        None lets the tool proceed normally.
        """
        if self._state is None:
            return None

        # Fast path: if kill is already armed, block before running checks.
        # Saves a bit of work and guarantees the directive shape is identical
        # across the "armed earlier" and "armed by this call's check" branches.
        if self._state.terminate_requested:
            return self._block_directive(
                self._state.terminate_reason or "caspase termination"
            )

        # Run checks (loop / cost / wall-clock / scope). If any fires Terminal,
        # state.terminate_requested flips inside bridge.on_pre_tool_call.
        on_pre_tool_call(self._state, tool_name, args)

        if self._state.terminate_requested:
            return self._block_directive(
                self._state.terminate_reason or "caspase termination"
            )

        return None

    def post_tool_call(self, tool_name: str, args: Any, result: Any) -> None:
        if self._state is None:
            return
        on_post_tool_call(self._state, tool_name, args, result)
        # If kill became armed during the tool, the next pre_tool_call will
        # block. No additional escalation needed here.

    def pre_llm_call(self, model: str) -> None:
        if self._state is None:
            return
        on_pre_llm_call(self._state, model)

    def post_api_request(
        self,
        model: str,
        usage: dict[str, Any],
        api_duration: float,
    ) -> None:
        if self._state is None:
            return
        input_tokens, output_tokens = _extract_token_counts(usage)
        on_post_api_request(self._state, model, input_tokens, output_tokens)

    def session_end(self) -> None:
        if self._state is None:
            return
        bridge_on_session_end(self._state)

        # Every async call below runs on the SAME session loop that owns the
        # shared httpx client and the background worker — no cross-loop reuse,
        # so no "Event loop is closed". If setup() never wired a loop (a failed
        # register, or a unit test that injects state directly), spin up a
        # throwaway one so the death cert is still best-effort posted. All of
        # this is best-effort: a teardown hiccup must never escape a Hermes hook.
        loop_thread = self._loop_thread or _SessionLoop()

        # 1. Death certificate. Render + save it locally on every kill (the
        #    autopsy is delivered even with no control plane); additionally
        #    POST it for archival only when online.
        if self._state.terminate_requested:
            if self._local_cert:
                self._emit_local_cert()
            if not self._state.offline:
                with contextlib.suppress(Exception):
                    loop_thread.run(self._post_death_cert_best_effort(), timeout=35.0)

        # 2. Stop the worker. Its final drain flushes every queued event
        #    (tool calls, symptoms, session_end). Stop BEFORE unregistering so
        #    that final drain still sees this agent's queue.
        with contextlib.suppress(Exception):
            loop_thread.run(BackgroundWorker.stop(), timeout=35.0)
        with contextlib.suppress(Exception):
            loop_thread.run(KillPendingPoller.stop(), timeout=10.0)
        with contextlib.suppress(Exception):
            loop_thread.run(self._client.aclose(), timeout=10.0)

        if self._state.agent_id:
            unregister_watcher(self._state.agent_id)

        # 3. Tear down the loop thread.
        loop_thread.close()
        self._loop_thread = None

    # --- helpers -------------------------------------------------------------

    def _block_directive(self, reason: str) -> dict[str, str]:
        """Build the Hermes block directive for an apoptosis kill.

        Hermes wraps ``message`` into ``{"error": message}`` and surfaces it
        as the tool's result. The wording asks the agent to stop — we cannot
        force the session to end, but the harm (further tool execution) is
        already prevented because the tool didn't run.
        """
        return {
            "action": "block",
            "message": (
                f"caspase apoptosis: this agent has been terminated by the "
                f"supervisor. Reason: {reason}. Do not retry; do not call "
                "other tools; end the session cleanly."
            ),
        }

    # --- local death cert ----------------------------------------------------

    def _emit_local_cert(self) -> None:
        """Render the death certificate to stderr and save it under
        ``~/.caspase/kills/``. Synchronous and best-effort — a rendering hiccup
        must never escape a Hermes hook."""
        if self._state is None:
            return
        # Windows consoles default to cp1252, which can't encode the cert's
        # box-drawing glyphs. Reconfigure stderr to UTF-8 (best-effort, with
        # replacement) so the write never raises — same guard the CLI uses.
        reconfigure = getattr(sys.stderr, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")
        try:
            cert = build_death_certificate(self._state)
            cost_line = _format_cost_line(self._state)
            sys.stderr.write("\n" + render_certificate(cert, cost_line=cost_line) + "\n")
            sys.stderr.flush()
        except Exception:
            logger.exception(
                "caspase: failed to render death certificate for agent %s",
                self._state.agent_id,
            )
            return
        try:
            path = save_certificate(cert, cost_line=cost_line)
            sys.stderr.write(f"caspase: death certificate saved to {path}\n")
            sys.stderr.flush()
        except Exception:
            logger.exception(
                "caspase: failed to save death certificate for agent %s",
                self._state.agent_id,
            )

    # --- death cert posting --------------------------------------------------

    async def _post_death_cert_best_effort(self) -> None:
        if self._state is None:
            return
        t0 = time.monotonic()
        try:
            payload = build_kill_event_payload(self._state)
        except Exception:
            logger.exception(
                "caspase: failed to build death certificate for agent %s",
                self._state.agent_id,
            )
            return
        try:
            result = await self._client.post_kill_event(self._state.agent_id, payload)
        except Exception:
            logger.exception(
                "caspase: failed to POST death certificate for agent %s",
                self._state.agent_id,
            )
            self._state.record_shutdown_step(
                "death_cert_post_failed",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
            return
        duration_ms = (time.monotonic() - t0) * 1000
        kill_event_id = result if isinstance(result, int) else result.id
        self._state.record_shutdown_step(
            "death_cert_posted" if not isinstance(result, int) else "death_cert_post_skipped_409",
            duration_ms=duration_ms,
            kill_event_id=kill_event_id,
        )
        logger.info(
            "caspase: death certificate posted for agent %s (kill_event=%s)",
            self._state.agent_id,
            kill_event_id,
        )


# --- cost formatting ---------------------------------------------------------


def _format_cost_line(state: WatcherState) -> str:
    """One-line cost summary for the local death cert, e.g.
    ``$0.42  ·  18.2k in / 2.1k out``. Reads the watcher's cumulative
    token/cost counters (which the cert itself doesn't carry)."""

    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    return (
        f"${state.total_cost_usd:.2f}  ·  "
        f"{_k(state.total_input_tokens)} in / {_k(state.total_output_tokens)} out"
    )


# --- token extraction --------------------------------------------------------


def _extract_token_counts(usage: dict[str, Any]) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from a Hermes post_api_request usage dict.

    Hermes' canonical shape is ``{"input_tokens": N, "output_tokens": M}``
    (see hermes_cli/hooks.py::_DEFAULT_PAYLOADS). We also tolerate OpenAI-
    style aliases (``prompt_tokens``/``completion_tokens``) in case Hermes
    surfaces a provider response shape unchanged for some backends.
    """
    if not usage:
        return 0, 0
    try:
        inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return 0, 0
    return inp, out
