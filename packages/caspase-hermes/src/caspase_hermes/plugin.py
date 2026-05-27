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
import logging
import time
from typing import Any

from caspase.apoptosis import Watchdog, build_kill_event_payload
from caspase.client import CaspaseClient
from caspase.policies import resolve_policy
from caspase.watcher import (
    BackgroundWorker,
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


class CaspasePlugin:
    """One per Hermes session. Owns the WatcherState lifecycle for that session."""

    def __init__(
        self,
        *,
        name: str,
        policy: str,
        metadata: dict[str, Any] | None = None,
        client: CaspaseClient,
    ) -> None:
        self._client = client
        self._name = name
        self._policy_name = policy
        self._metadata = metadata or {}
        self._state: WatcherState | None = None

    async def setup(self) -> None:
        """Register the agent with the control plane and wire up the watcher."""
        resolved_policy = resolve_policy(self._policy_name)

        registration = await self._client.register_agent(
            name=self._name,
            policy_name=resolved_policy.name,
            metadata=self._metadata,
        )

        state = WatcherState(
            agent_id=registration.agent_id,
            name=self._name,
            policy=resolved_policy,
        )
        state.watchdog = Watchdog(
            state,
            grace_seconds=resolved_policy.thresholds.cooperative_grace_seconds,
        )
        register_watcher(state)
        ensure_worker_started(self._client)
        self._state = state

        state.record_lifecycle("registered", agent_id=str(state.agent_id))
        logger.info(
            "caspase: watching %r (id=%s, policy=%s)",
            self._name,
            state.agent_id,
            resolved_policy.name,
        )

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
        # Best-effort: flush death cert if apoptosis fired this session.
        # asyncio.run() is safe here — session_end() is a sync Hermes hook
        # called outside of any running event loop.
        if self._state.terminate_requested:
            asyncio.run(self._post_death_cert_best_effort())
        if self._state.agent_id:
            unregister_watcher(self._state.agent_id)
        asyncio.run(BackgroundWorker.stop())

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
