"""LangChain callback handler: agent lifecycle events → ``WatcherState``.

This is the LangGraph analog of ``caspase_hermes.bridge`` — the thin adapter
that feeds a runtime's lifecycle events into the Caspase engine. The engine
itself (loop / cost / wall-clock / tool-scope checks) lives in ``caspase`` and
is shared verbatim; only the *boundary* differs per runtime.

The key difference from the Hermes adapter is the **kill mechanism**. Hermes
hooks return a block directive (``{"action": "block", ...}``) that the runtime
turns into a tool error. LangChain has no such return channel — the documented
way to abort is to *raise* from a callback with ``raise_error = True`` so the
exception propagates out of LangChain's callback runner instead of being
swallowed. We raise :class:`caspase.exceptions.CaspaseTerminated`, which the
``watch()`` wrapper catches to post the death certificate.

Checkpoint sites
----------------
The callbacks that fire *between* units of agent work — ``on_chain_start``
(LangGraph fires this per node) and ``on_tool_start`` (before any tool runs) —
call ``_checkpoint(state)`` first. If ``state.terminate_requested`` is set,
``CaspaseTerminated`` is raised. This is the L1 cooperative-termination
contract: finish the current unit, abort at the next boundary. The flag itself
is flipped by the symptom checks (``_apply_results``) or the manual-kill poller;
this handler only *honors* it.

All event recording goes through ``WatcherState``, which queues events for the
shared ``BackgroundWorker`` to flush — we never POST from a hot-path callback.
LangChain's contract is "callbacks may run on any thread, sync or async";
``WatcherState`` is thread-safe via its internal lock, so this holds regardless
of which loop the customer runs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from caspase.checks import (
    Terminal,
    Warning,
    apply_grants,
    check_tool_scope,
    run_all,
)
from caspase.exceptions import CaspaseTerminated
from caspase.watcher import WatcherState
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger("caspase_langgraph.callback")


def _checkpoint(state: WatcherState) -> None:
    """Raise CaspaseTerminated if the apoptosis flag is set on the given state.

    Called at every checkpoint site (chain start, tool start). The flag is
    flipped elsewhere (symptom checks / manual-kill poller); here we honor it.
    """
    if state.terminate_requested:
        raise CaspaseTerminated(
            state.terminate_reason or "terminated",
            kill_event_id=state.terminate_kill_event_id,
        )


def _apply_results(state: WatcherState, results: list[Terminal | Warning]) -> None:
    """Record symptom events and flip the apoptosis flag on the first Terminal.

    **First-cause wins:** if the flag is already set, subsequent Terminals
    still emit a symptom event (audit), but ``terminate_reason`` is not
    overwritten — the death cert should show what *first* caused apoptosis.

    **Grants:** ``apply_grants`` demotes Terminals covered by an active grant
    to Warnings (carrying ``grant_id``); the Warning is recorded for audit but
    does not trip the flag.

    The flag flip is bookkeeping only — the actual raise happens at the next
    ``_checkpoint`` call (chain_start or tool_start), honoring the cooperative
    "abort at the next boundary" contract.
    """
    results = apply_grants(results, state.grants)
    for r in results:
        severity = "terminal" if isinstance(r, Terminal) else "warning"
        try:
            state.record_symptom(
                symptom=r.symptom,
                severity=severity,
                reason=r.reason,
                detail=r.detail,
            )
        except Exception:
            logger.exception("failed to record symptom event")

        if isinstance(r, Terminal) and not state.terminate_requested:
            # request_termination is idempotent (first-cause wins) and sets
            # the threading.Event the L2 watchdog sleeps on, so the watchdog
            # wakes immediately rather than waiting for its next poll.
            state.request_termination(r.reason)
            logger.warning(
                "caspase: agent %s entering apoptosis: %s (%s)",
                state.agent_id,
                r.symptom.value,
                r.reason,
            )


def _arm_watchdog_if_possible(state: WatcherState) -> None:
    """Capture the running loop + current task into the L2 watchdog.

    Called from ``on_chain_start``. No-op if there is no watchdog (synthetic
    test state) or we're not inside an asyncio context (sync callback site —
    L2 simply isn't available there; cooperative L1 still works). Failures
    here never propagate — arming is defense-in-depth, not a hard dependency.
    """
    if state.watchdog is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # sync context; L2 not applicable
    task = asyncio.current_task()
    if task is None:
        return
    try:
        state.watchdog.arm(loop, task)
    except Exception:
        logger.exception("watchdog arm failed (continuing without L2)")


class CaspaseCallbackHandler(BaseCallbackHandler):
    """Attach to any LangChain runnable: ``runnable.with_config(callbacks=[handler])``.

    Holds a reference to one ``WatcherState``; one handler per watched agent.
    Usually you don't construct this directly — :func:`caspase_langgraph.watch`
    builds it for you and wires the control-plane lifecycle around it.
    """

    # LangChain inspects these attributes to decide how to invoke the handler.
    # ``raise_error=True`` lets ``CaspaseTerminated`` propagate out of the
    # callback runner — otherwise it would be swallowed and the apoptosis
    # signal would never reach the agent's stack frame.
    raise_error: bool = True
    run_inline: bool = True

    def __init__(self, state: WatcherState) -> None:
        super().__init__()
        self.state = state

    # --- tool calls -------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # 1. Checkpoint BEFORE anything — once dying, block new tool calls.
        _checkpoint(self.state)

        tool_name = (serialized or {}).get("name") or "<unknown>"

        # 2. Tool-scope check runs BEFORE we record / let the tool run. If the
        # tool is out of scope, flip the flag and re-checkpoint to raise before
        # LangChain dispatches it.
        scope_result = check_tool_scope(tool_name, self.state.policy)
        if isinstance(scope_result, Terminal | Warning):
            _apply_results(self.state, [scope_result])
            _checkpoint(self.state)  # raises if scope just set the flag

        # 3. Record the call (updates loop ring buffer + queues event).
        params = inputs if inputs is not None else input_str
        try:
            self.state.record_tool_call(tool_name, params)
        except Exception:
            logger.exception("on_tool_start: failed to record")

        # 4. Run the state-only checks (loop / cost / wall_clock). Any Terminal
        # flips the flag; the NEXT checkpoint raises. We don't raise from here —
        # let the current tool dispatch complete so counters reflect reality.
        _apply_results(self.state, run_all(self.state, self.state.policy))

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self.state.record_lifecycle(
                "tool_error",
                run_id=str(run_id),
                error=f"{type(error).__name__}: {error}",
            )
        except Exception:
            logger.exception("on_tool_error: failed to record")

    # --- LLM calls --------------------------------------------------------

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        # LangChain stuffs token usage in different places per provider. Try
        # the common shapes; fall back to zero (cost check still works, just
        # under-counts that call).
        model, input_tokens, output_tokens = _extract_token_usage(response)
        try:
            self.state.record_llm_call(model, input_tokens, output_tokens)
        except Exception:
            logger.exception("on_llm_end: failed to record")

        # Cost / wall-clock can fire here; loop check is a no-op (no new tool
        # sig). Flag flip; next checkpoint raises.
        _apply_results(self.state, run_all(self.state, self.state.policy))

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self.state.record_lifecycle(
                "llm_error",
                run_id=str(run_id),
                error=f"{type(error).__name__}: {error}",
            )
        except Exception:
            logger.exception("on_llm_error: failed to record")

    # --- chain lifecycle (LangGraph fires on_chain_start per node) --------

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # Checkpoint at every chain boundary, top-level or nested. The
        # LangGraph runtime fires this on each node start, so this is our
        # primary apoptosis checkpoint for LangGraph-based agents.
        _checkpoint(self.state)

        # Arm the L2 watchdog if one is attached. Done on *every* chain_start
        # (not just top-level) so a new ainvoke running in a different task
        # gets the right task captured. arm() is idempotent.
        _arm_watchdog_if_possible(self.state)

        # Only *record* top-level chain starts — child chains are too noisy.
        if parent_run_id is not None:
            return
        name = (serialized or {}).get("name") or "chain"
        try:
            self.state.record_lifecycle("chain_start", chain=name)
        except Exception:
            logger.exception("on_chain_start: failed to record")

        # Run checks at top-level chain boundaries too — catches wall-clock
        # runaway between LLM/tool boundaries (e.g. a node spending 30 minutes
        # inside a custom sync function that never trips another check).
        _apply_results(self.state, run_all(self.state, self.state.policy))


# --- helpers --------------------------------------------------------------


def _extract_token_usage(response: LLMResult) -> tuple[str, int, int]:
    """Best-effort token-usage extraction across LangChain providers.

    Returns ``(model, input_tokens, output_tokens)``. Defaults to
    ``("unknown", 0, 0)`` when no usable shape is present.
    """
    model = "unknown"
    input_tokens = 0
    output_tokens = 0

    llm_output = response.llm_output or {}
    if isinstance(llm_output, dict):
        # OpenAI / Anthropic legacy shape.
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens = int(
                usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            )
            output_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
        model = str(llm_output.get("model_name") or llm_output.get("model") or model)

    # Modern langchain >=0.3 exposes usage_metadata on AIMessage chunks.
    for generation_list in response.generations:
        for gen in generation_list:
            msg = getattr(gen, "message", None)
            if msg is None:
                continue
            usage_md = getattr(msg, "usage_metadata", None)
            if usage_md:
                input_tokens = int(usage_md.get("input_tokens", input_tokens))
                output_tokens = int(usage_md.get("output_tokens", output_tokens))
            response_md = getattr(msg, "response_metadata", None) or {}
            if isinstance(response_md, dict) and "model_name" in response_md:
                model = str(response_md["model_name"])

    return model, input_tokens, output_tokens
