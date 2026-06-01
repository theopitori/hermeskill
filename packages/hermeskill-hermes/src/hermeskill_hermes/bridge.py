"""Thin adapter: Hermes hook payloads → WatcherState mutations.

Each function in this module translates one Hermes lifecycle event into the
appropriate ``WatcherState`` mutation (record_tool_call, record_llm_call, etc.)
and then runs the Hermeskill checks, returning a kill verdict if apoptosis fires.

These are **pure functions over state** — they do not talk to the control
plane, do not raise, and do not side-effect outside the passed ``state``. The
plugin layer (``plugin.py``) owns control-plane interaction and translates a
kill verdict into Hermes' block directive.

Hermes hook payload shapes (v0.14, from ``hermes_cli/hooks.py::_DEFAULT_PAYLOADS``):

    pre_tool_call(*, tool_name, args, session_id, task_id, tool_call_id)
    post_tool_call(*, tool_name, args, session_id, task_id, tool_call_id,
                   result, duration_ms)
    pre_llm_call(*, session_id, user_message, conversation_history,
                 is_first_turn, model, platform)
    post_api_request(*, session_id, task_id, platform, model, provider,
                     base_url, api_mode, api_call_count, api_duration,
                     finish_reason, message_count, response_model,
                     usage, assistant_content_chars, assistant_tool_call_count)
    on_session_end(*, session_id)

We register against ``post_api_request`` rather than ``post_llm_call`` because
the canonical ``post_llm_call`` payload carries no token-usage information
in v0.14 — usage lands on ``post_api_request.usage``.
"""

from __future__ import annotations

import logging
from typing import Any

from hermeskill.checks import (
    Terminal,
    Warning,
    apply_grants,
    check_tool_scope,
    run_all,
)
from hermeskill.watcher import WatcherState

logger = logging.getLogger("hermeskill_hermes.bridge")


def on_pre_tool_call(
    state: WatcherState,
    tool_name: str,
    args: Any,
) -> list[Terminal | Warning]:
    """Pre-tool boundary checkpoint.

    1. Tool-scope check — fires BEFORE the tool runs (scope violation is
       recorded and the Terminal is returned for the caller to act on).
    2. Record the call (loop ring buffer + event queue).
    3. Run state checks (loop, cost, wall-clock).

    Returns all non-Healthy verdicts (with grants applied). An empty list
    means all checks passed. The caller (plugin.py) translates any Terminal
    into Hermes' block directive.
    """
    verdicts: list[Terminal | Warning] = []

    # Scope check runs first — before recording, so a scope violation
    # doesn't pollute the loop ring buffer with tools we shouldn't be
    # tracking. Must run before record_tool_call for this ordering to hold.
    scope = check_tool_scope(tool_name, state.policy)
    if isinstance(scope, (Terminal, Warning)):
        verdicts.append(scope)

    try:
        state.record_tool_call(tool_name, args)
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
                "hermeskill: agent %s entering apoptosis: %s (%s)",
                state.agent_id,
                v.symptom.value,
                v.reason,
            )

    return [v for v in all_verdicts if isinstance(v, Terminal | Warning)]


def on_post_tool_call(
    state: WatcherState,
    tool_name: str,
    args: Any,
    result: Any,
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
                "hermeskill: agent %s entering apoptosis post-tool: %s (%s)",
                state.agent_id,
                v.symptom.value,
                v.reason,
            )


def on_pre_llm_call(state: WatcherState, model: str) -> None:
    """Pre-LLM — lifecycle marker. Token info lands on post_api_request."""
    try:
        state.record_lifecycle("llm_start", model=model)
    except Exception:
        logger.exception("bridge.on_pre_llm_call: failed to record")


def on_post_api_request(
    state: WatcherState,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Post-API-request — update token/cost counters from the ``usage`` dict
    Hermes carries on this hook; run checks."""
    try:
        state.record_llm_call(model, input_tokens, output_tokens)
    except Exception:
        logger.exception("bridge.on_post_api_request: failed to record")

    verdicts = apply_grants(run_all(state, state.policy), state.grants)
    for v in verdicts:
        if isinstance(v, Terminal) and not state.terminate_requested:
            state.request_termination(v.reason)
            logger.warning(
                "hermeskill: agent %s entering apoptosis post-api-request: %s (%s)",
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
