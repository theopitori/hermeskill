"""Hermes plugin hook handlers for Caspase.

This module wires the Hermes hook API to the Caspase apoptosis engine. One
`CaspasePlugin` instance is created per Hermes session by `register()` in
`__init__.py`.

Kill path (L1 via tool_override):
  Hermes v0.14 hooks are non-blocking — the runtime catches `Exception` from
  any hook and logs it without crashing the agent. We therefore cannot raise
  from a hook to abort a tool call.

  Instead we use `ctx.tool_override(tool_name, stub)` (added in v0.14,
  PR #26759) to swap the about-to-run tool for a `_KillStub` callable. The
  stub raises `SystemExit` — which derives from `BaseException`, NOT
  `Exception`, so it propagates even through an `except Exception` guard in
  the Hermes runtime.

  The override is set lazily: on every `pre_tool_call`, if a kill directive is
  pending (state.terminate_requested), we register the override for THAT
  tool_name before returning. The very next execution of that tool hits the
  stub and triggers controlled shutdown.

  For the `on_session_end` path (graceful exits, `/new`, `/reset`):
  The hook flushes the death cert to the control plane and tears down the
  background worker cleanly.

Background worker lifecycle:
  `register()` calls `caspase.watcher.ensure_worker_started(client)`.
  This starts the shared per-process `BackgroundWorker` (heartbeats + event
  drain) AND the `KillPendingPoller` (manual-kill delivery). Both singletons
  survive across Hermes sessions in the same process — safe because they only
  reference the module-level `_REGISTRY`.

  On `on_session_end`, the plugin calls `BackgroundWorker.stop()` to flush
  remaining events before Hermes tears the session down.
"""

from __future__ import annotations

import asyncio
import logging
import sys
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
    on_post_llm_call,
    on_post_tool_call,
    on_pre_llm_call,
    on_pre_tool_call,
)
from caspase_hermes.bridge import (
    on_session_end as bridge_on_session_end,
)

logger = logging.getLogger("caspase_hermes.plugin")


class _KillStub:
    """Drop-in replacement for any Hermes tool. Raises SystemExit (BaseException)
    so it propagates even through `except Exception` guards in the Hermes runtime.

    SystemExit is the right signal: it triggers Hermes's normal teardown path,
    which fires `on_session_end` so Caspase can post the death certificate.
    """

    def __init__(self, tool_name: str, reason: str) -> None:
        self._tool_name = tool_name
        self._reason = reason

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        logger.warning(
            "caspase: kill stub fired for tool %r — reason: %s",
            self._tool_name,
            self._reason,
        )
        sys.exit(0)


class CaspasePlugin:
    """One per Hermes session. Owns the WatcherState lifecycle for that session."""

    def __init__(
        self,
        ctx: Any,
        *,
        name: str,
        policy: str,
        metadata: dict[str, Any] | None = None,
        client: CaspaseClient,
    ) -> None:
        self._ctx = ctx
        self._client = client
        self._name = name
        self._policy_name = policy
        self._metadata = metadata or {}
        self._state: WatcherState | None = None
        self._registered_overrides: set[str] = set()

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

    def pre_tool_call(self, tool_name: str, inputs: Any) -> None:
        if self._state is None:
            return
        # If already dying, arm override immediately for this tool.
        if self._state.terminate_requested:
            self._arm_kill_override(tool_name)
            return
        verdicts = on_pre_tool_call(self._state, tool_name, inputs)
        if verdicts and self._state.terminate_requested:
            self._arm_kill_override(tool_name)

    def post_tool_call(self, tool_name: str, inputs: Any, output: Any) -> None:
        if self._state is None:
            return
        on_post_tool_call(self._state, tool_name, inputs, output)
        if self._state.terminate_requested:
            self._arm_kill_override(tool_name)

    def pre_llm_call(self, model: str, messages: Any) -> None:
        if self._state is None:
            return
        on_pre_llm_call(self._state, model, messages)

    def post_llm_call(self, model: str, response: Any) -> None:
        if self._state is None:
            return
        input_tokens, output_tokens = _extract_token_counts(response)
        on_post_llm_call(self._state, model, input_tokens, output_tokens)

    def session_end(self) -> None:
        if self._state is None:
            return
        bridge_on_session_end(self._state)
        # Best-effort: flush death cert if apoptosis fired this session.
        if self._state.terminate_requested:
            asyncio.get_event_loop().run_until_complete(
                self._post_death_cert_best_effort()
            )
        if self._state.agent_id:
            unregister_watcher(self._state.agent_id)
        asyncio.get_event_loop().run_until_complete(BackgroundWorker.stop())

    # --- kill-override wiring ------------------------------------------------

    def _arm_kill_override(self, tool_name: str) -> None:
        """Register a kill stub for tool_name if not already done.

        Idempotent — safe to call on every pre_tool_call when dying.

        Hermes v0.14 ctx.tool_override(tool_name, callable) API:
          Replaces the named tool's implementation for the duration of this
          session. The replacement callable receives the same args the real
          tool would have received.
        """
        if tool_name in self._registered_overrides:
            return
        # _arm_kill_override is only reachable after setup() has populated
        # _state; the type-narrowing assert makes that contract explicit
        # for mypy and surfaces a useful error if the invariant ever breaks.
        assert self._state is not None, "_arm_kill_override called before setup()"
        reason = self._state.terminate_reason or "caspase termination"
        stub = _KillStub(tool_name, reason)
        try:
            self._ctx.tool_override(tool_name, stub)
            self._registered_overrides.add(tool_name)
            logger.warning(
                "caspase: kill override armed for tool %r (reason: %s)",
                tool_name,
                reason,
            )
        except Exception:
            logger.exception(
                "caspase: failed to arm kill override for tool %r; "
                "agent may continue running until next session boundary",
                tool_name,
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


# --- token extraction --------------------------------------------------------


def _extract_token_counts(response: Any) -> tuple[int, int]:
    """Best-effort token extraction from a Hermes LLM response object.

    Hermes wraps provider responses in a common shape; we try a few known
    locations (usage, usage_metadata, token_usage) and default to 0/0.
    """
    if response is None:
        return 0, 0
    # Try direct attributes first (e.g. Anthropic / OpenAI via Hermes wrapper)
    for attr in ("usage", "usage_metadata", "token_usage"):
        u = getattr(response, attr, None)
        if u is None and isinstance(response, dict):
            u = response.get(attr)
        if u is not None:
            if isinstance(u, dict):
                inp = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
                out = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
            else:
                inp = int(getattr(u, "input_tokens", None) or getattr(u, "prompt_tokens", None) or 0)
                out = int(getattr(u, "output_tokens", None) or getattr(u, "completion_tokens", None) or 0)
            if inp > 0 or out > 0:
                return inp, out
    return 0, 0
