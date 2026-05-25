"""Thin adapter: Hermes hook payloads → WatcherState mutations.

Each function in this module translates one Hermes lifecycle event into the
appropriate `WatcherState` mutation (record_tool_call, record_llm_call, etc.)
and then runs the Caspase checks, returning a kill verdict if apoptosis fires.

These are **pure functions over state** — they do not talk to the control
plane, do not raise, and do not side-effect outside the passed `state`. The
plugin layer (plugin.py) owns control-plane interaction and the tool_override
kill trigger.

Hermes hook callback signatures assumed (v0.14):

    pre_tool_call(ctx, tool_name: str, inputs: dict) -> None
    post_tool_call(ctx, tool_name: str, inputs: dict, output: Any) -> None
    pre_llm_call(ctx, messages: list, model: str) -> None
    post_llm_call(ctx, messages: list, model: str, response: Any) -> None
    on_session_end(ctx) -> None

The `ctx` object exposes:
    ctx.register_hook(event_name, callback)  — called at register() time
    ctx.tool_override(tool_name, replacement) — swap a tool implementation

Usage of ctx is intentionally isolated to plugin.py; bridge.py only touches
WatcherState so that unit-testing the bridge never requires a live Hermes ctx.
"""

from __future__ import annotations

import logging
from typing import Any

from caspase.checks import (
    Terminal,
    Warning,
    apply_grants,
    check_tool_scope,
    run_all,
)
from caspase.watcher import WatcherState

logger = logging.getLogger("caspase_hermes.bridge")


def on_pre_tool_call(
    state: WatcherState,
    tool_name: str,
    inputs: Any,
) -> list[Terminal | Warning]:
    """Pre-tool boundary checkpoint.

    1. Tool-scope check — fires BEFORE the tool runs (same semantics as the
       LangChain on_tool_start handler; scope violation is recorded and the
       Terminal is returned for the caller to act on).
    2. Record the call (loop ring buffer + event queue).
    3. Run state checks (loop, cost, wall-clock).

    Returns all non-Healthy verdicts (with grants applied). An empty list
    means all checks passed. The caller (plugin.py) decides whether to
    trigger tool_override based on the returned list.
    """
    verdicts: list[Terminal | Warning] = []

    # Scope check runs first — before recording, so a scope violation
    # doesn't pollute the loop ring buffer with tools we shouldn't be
    # tracking. Mirrors langchain.py's on_tool_start ordering.
    scope = check_tool_scope(tool_name, state.policy)
    if not isinstance(scope, type(None)) and not _is_healthy(scope):
        verdicts.append(scope)

    try:
        state.record_tool_call(tool_name, inputs)
    except Exception:
        logger.exception("bridge.on_pre_tool_call: failed to record tool call")

    verdicts.extend(run_all(state, state.policy))
    all_verdicts = apply_grants(verdicts, state.grants)

    for v in all_verdicts:
        severity = "terminal" if isinstance(v, Terminal) else "warning"
        try:
            state.record_symptom(
                symptom=v.symptom,
                severity=severity,
                reason=v.reason,
                detail=v.detail,
            )
        except Exception:
            logger.exception("bridge.on_pre_tool_call: failed to record symptom")

        if isinstance(v, Terminal) and not state.terminate_requested:
            state.request_termination(v.reason)
            logger.warning(
                "caspase: agent %s entering apoptosis: %s (%s)",
                state.agent_id,
                v.symptom.value,
                v.reason,
            )

    return [v for v in all_verdicts if isinstance(v, Terminal | Warning)]


def on_post_tool_call(
    state: WatcherState,
    tool_name: str,
    inputs: Any,
    output: Any,
) -> None:
    """Post-tool — record outcome; run checks again (cost/wall_clock may have
    ticked while the tool ran)."""
    try:
        state.record_lifecycle("tool_end", tool=tool_name)
    except Exception:
        logger.exception("bridge.on_post_tool_call: failed to record")

    verdicts = apply_grants(run_all(state, state.policy), state.grants)
    for v in verdicts:
        if isinstance(v, Terminal) and not state.terminate_requested:
            state.request_termination(v.reason)
            logger.warning(
                "caspase: agent %s entering apoptosis post-tool: %s (%s)",
                state.agent_id,
                v.symptom.value,
                v.reason,
            )


def on_pre_llm_call(
    state: WatcherState,
    model: str,
    messages: Any,
) -> None:
    """Pre-LLM — snapshot the model name so post_llm_call can attribute cost."""
    try:
        state.record_lifecycle("llm_start", model=model)
    except Exception:
        logger.exception("bridge.on_pre_llm_call: failed to record")


def on_post_llm_call(
    state: WatcherState,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Post-LLM — update token/cost counters; run checks."""
    try:
        state.record_llm_call(model, input_tokens, output_tokens)
    except Exception:
        logger.exception("bridge.on_post_llm_call: failed to record")

    verdicts = apply_grants(run_all(state, state.policy), state.grants)
    for v in verdicts:
        if isinstance(v, Terminal) and not state.terminate_requested:
            state.request_termination(v.reason)
            logger.warning(
                "caspase: agent %s entering apoptosis post-llm: %s (%s)",
                state.agent_id,
                v.symptom.value,
                v.reason,
            )


def on_session_end(state: WatcherState) -> None:
    """Session teardown — record lifecycle step; the plugin layer flushes
    the event queue and tears down the background worker."""
    try:
        state.record_lifecycle("session_end")
        state.record_shutdown_step("hermes_session_ended")
    except Exception:
        logger.exception("bridge.on_session_end: failed to record")


# --- helpers ------------------------------------------------------------------


def _is_healthy(result: Any) -> bool:
    """Duck-type check: True for the HEALTHY sentinel and any Healthy instance."""
    from caspase.checks import Healthy
    return isinstance(result, Healthy)
