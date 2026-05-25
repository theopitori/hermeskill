"""Tests for M2.3 — L1 cooperative apoptosis + L3 tool quarantine via the
LangChain callback handler.

What's covered:

- Each symptom (loop / cost / wall-clock / tool-scope) trips the apoptosis
  flag through the handler when its boundary check fires
- The flag flip is **not** the raise — the next checkpoint is. We verify
  both: the flag goes True, then the next on_tool_start / on_chain_start
  raises `CaspaseTerminated`
- **L3 tool quarantine**: tool-scope violation raises *inside on_tool_start*
  before the tool dispatches; once the flag is set (from any cause), every
  subsequent on_tool_start raises immediately — no further tools run
- Symptom events are queued with severity/reason/detail intact (these go
  to `caspase logs` + the M2.5 death cert)
- First-cause wins: if two Terminals fire, the first one's reason sticks;
  the second still records a symptom event but doesn't overwrite the flag
- Manual flag-flip (M4 path) bypasses all checks — verified at chain_start
- End-to-end: a LangGraph that records 6 identical tool calls dies
  cooperatively at the next chain boundary (no force-kill required)
"""

from __future__ import annotations

import time
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from caspase import CaspaseTerminated
from caspase.langchain import CaspaseCallbackHandler, _apply_results
from caspase.langgraph import with_caspase
from caspase.policies import resolve_policy
from caspase.types import EventType, Policy, PolicyThresholds, SymptomType
from caspase.watcher import WatcherState
from langchain_core.outputs import LLMResult
from langgraph.graph import END, START, StateGraph

# --- helpers --------------------------------------------------------------


def _state(policy: Policy | None = None) -> WatcherState:
    return WatcherState(
        agent_id=uuid4(),
        name="t",
        policy=policy or resolve_policy("coding-default"),
    )


def _policy(**overrides: object) -> Policy:
    """Coding-default with selected thresholds overridden."""
    base = resolve_policy("coding-default")
    fields = base.thresholds.model_dump()
    fields.update(overrides)
    return base.model_copy(update={"thresholds": PolicyThresholds(**fields)})


def _symptom_events(state: WatcherState) -> list[Any]:
    """Drain and return only SYMPTOM events."""
    return [e for e in state.drain_events() if e.type == EventType.SYMPTOM]


# --- loop check via handler ----------------------------------------------


def test_loop_terminal_flips_flag_via_handler() -> None:
    """Five identical tool calls (cap=5) → flag set on the 5th."""
    p = _policy(max_loop_repeats=5, loop_window_actions=20)
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    # First 4 calls: flag stays clear.
    for _ in range(4):
        h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())
        assert not s.terminate_requested

    # 5th call: ring buffer hits threshold inside on_tool_start, flag flips,
    # but the raise comes on the NEXT checkpoint, not this one.
    h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())
    assert s.terminate_requested
    assert s.terminate_reason is not None
    assert "repeated" in s.terminate_reason


def test_next_tool_start_after_loop_raises() -> None:
    p = _policy(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    for _ in range(3):
        h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())
    assert s.terminate_requested

    # The NEXT on_tool_start raises — L3 quarantine.
    with pytest.raises(CaspaseTerminated):
        h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())


def test_next_chain_start_after_loop_raises() -> None:
    """on_chain_start is also a checkpoint — apoptosis works between nodes
    even without a tool call."""
    p = _policy(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    for _ in range(3):
        h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())
    assert s.terminate_requested

    with pytest.raises(CaspaseTerminated):
        h.on_chain_start(serialized={"name": "node"}, inputs={}, run_id=uuid4())


# --- cost / tokens via on_llm_end ----------------------------------------


def test_cost_runaway_flips_flag_at_llm_end() -> None:
    p = _policy(max_cost_usd=0.0001, max_tokens_per_run=10**9)
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    # Feed an LLMResult with usage that produces >$0.0001 cost.
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration

    msg = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 100_000, "output_tokens": 50_000, "total_tokens": 150_000},
        response_metadata={"model_name": "claude-haiku-4-5"},
    )
    response = LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={})
    h.on_llm_end(response, run_id=uuid4())

    assert s.terminate_requested
    assert s.terminate_reason is not None
    assert "cost" in s.terminate_reason.lower()


def test_token_cap_alone_trips_at_llm_end() -> None:
    """Even with zero-cost (unknown model), the token cap fires."""
    p = _policy(max_cost_usd=10_000.0, max_tokens_per_run=100)
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration

    msg = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 80, "output_tokens": 50, "total_tokens": 130},
        response_metadata={"model_name": "totally-fake-model"},  # priced at $0
    )
    response = LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={})
    h.on_llm_end(response, run_id=uuid4())

    assert s.terminate_requested
    assert "tokens" in (s.terminate_reason or "")


# --- wall-clock via on_chain_start ---------------------------------------


def test_wall_clock_fires_at_chain_start() -> None:
    """Long-running agent: chain_start runs run_all → wall_clock Terminal."""
    p = _policy(max_runtime_seconds=1)
    s = _state(p)
    s.started_monotonic = time.monotonic() - 60  # 60s elapsed
    h = CaspaseCallbackHandler(s)

    # The FIRST chain_start does _checkpoint (clear) → records → run_all
    # → flips flag. The raise happens at the NEXT checkpoint.
    h.on_chain_start(serialized={"name": "main"}, inputs={}, run_id=uuid4())
    assert s.terminate_requested
    assert "runtime" in (s.terminate_reason or "")

    with pytest.raises(CaspaseTerminated):
        h.on_chain_start(serialized={"name": "next"}, inputs={}, run_id=uuid4())


# --- tool-scope: L3 quarantine, blocks BEFORE dispatch -------------------


def test_tool_scope_violation_raises_at_tool_start() -> None:
    """Tool not in allowlist: raise BEFORE the tool runs. The whole point
    of L3 is that the disallowed action never dispatches."""
    p = resolve_policy("strict")  # allowlist: read_file, search
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    with pytest.raises(CaspaseTerminated, match="not in policy allowlist"):
        h.on_tool_start(
            serialized={"name": "rm_rf"}, input_str="", run_id=uuid4()
        )

    # And the flag is now set so subsequent allowed tools are blocked too.
    assert s.terminate_requested
    with pytest.raises(CaspaseTerminated):
        h.on_tool_start(
            serialized={"name": "read_file"}, input_str="", run_id=uuid4()
        )


def test_tool_scope_violation_records_no_tool_call_event() -> None:
    """Critical: the disallowed tool must NOT show up as a recorded tool
    call. The ring buffer + event log should only contain things that
    actually happened."""
    p = resolve_policy("strict")
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    with pytest.raises(CaspaseTerminated):
        h.on_tool_start(
            serialized={"name": "rm_rf"}, input_str="", run_id=uuid4()
        )

    events = s.drain_events()
    tool_calls = [e for e in events if e.type == EventType.TOOL_CALL]
    assert tool_calls == [], "disallowed tool was recorded as if it ran"
    # But a symptom event for the violation IS recorded.
    symptoms = [e for e in events if e.type == EventType.SYMPTOM]
    assert len(symptoms) == 1
    assert symptoms[0].payload["symptom"] == SymptomType.TOOL_SCOPE_VIOLATION.value


def test_tool_scope_allowed_tool_proceeds_normally() -> None:
    """Sanity: allowed tools record normally, no flag flip."""
    p = resolve_policy("strict")
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    h.on_tool_start(serialized={"name": "read_file"}, input_str="x", run_id=uuid4())
    assert not s.terminate_requested
    events = s.drain_events()
    tool_calls = [e for e in events if e.type == EventType.TOOL_CALL]
    assert len(tool_calls) == 1


# --- symptom event payload -----------------------------------------------


def test_symptom_event_payload_shape() -> None:
    """The symptom event powers death-cert + `caspase logs`. Verify the
    shape so M2.5 / CLI can rely on it."""
    p = _policy(max_loop_repeats=2, loop_window_actions=10)
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())
    h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())

    symptoms = _symptom_events(s)
    assert len(symptoms) == 1
    p_payload = symptoms[0].payload
    assert p_payload["symptom"] == SymptomType.LOOP.value
    assert p_payload["severity"] == "terminal"
    assert "reason" in p_payload
    assert "detail" in p_payload
    assert p_payload["detail"]["count"] == 2
    assert p_payload["detail"]["max_loop_repeats"] == 2


def test_warning_is_recorded_with_warning_severity() -> None:
    """`Warning` results (reserved for M5 grant suppression) must also flow
    to the event stream, with severity='warning' — so audit shows what was
    suppressed by which grant."""
    from caspase.checks import Warning

    s = _state()
    w = Warning(
        symptom=SymptomType.TOOL_SCOPE_VIOLATION,
        reason="suppressed by grant gr_abc",
        detail={"grant_id": "gr_abc"},
    )
    _apply_results(s, [w])
    assert not s.terminate_requested  # warnings don't kill
    symptoms = _symptom_events(s)
    assert len(symptoms) == 1
    assert symptoms[0].payload["severity"] == "warning"


# --- first-cause-wins ----------------------------------------------------


def test_first_cause_wins() -> None:
    """If two checks fire on the same boundary, the first sets the reason;
    the second still records a symptom but doesn't overwrite. The death
    cert should show what *first* killed the agent."""
    p = _policy(
        max_loop_repeats=3,
        loop_window_actions=20,
        max_cost_usd=0.001,
    )
    s = _state(p)
    h = CaspaseCallbackHandler(s)

    # 2 healthy calls fill the buffer to count=2 (under threshold).
    for _ in range(2):
        h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())
    assert not s.terminate_requested
    s.drain_events()  # discard non-symptom events so the assertion below is clean

    # Bump cost OVER cap NOW, then fire the 3rd identical tool call. In
    # the resulting run_all: loop fires (count=3) AND cost fires (over
    # cap) — same invocation, so first-cause-wins applies between them.
    # run_all order is loop → cost → wall_clock; loop should win.
    s.total_cost_usd = 100.0
    h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())

    assert s.terminate_requested
    assert "repeated" in (s.terminate_reason or ""), (
        f"expected loop reason to win, got: {s.terminate_reason!r}"
    )

    # Both symptoms recorded (audit completeness).
    symptoms = _symptom_events(s)
    kinds = {e.payload["symptom"] for e in symptoms}
    assert SymptomType.LOOP.value in kinds
    assert SymptomType.TOKEN_RUNAWAY.value in kinds


def test_already_dying_records_no_new_symptoms_for_same_cause() -> None:
    """If the flag is already set, the *next* on_tool_start raises immediately
    — we should NOT spend cycles running checks again or duplicating the
    symptom in the event log."""
    s = _state()
    s.terminate_requested = True
    s.terminate_reason = "previously set"
    h = CaspaseCallbackHandler(s)

    with pytest.raises(CaspaseTerminated, match="previously set"):
        h.on_tool_start(serialized={"name": "read_file"}, input_str="", run_id=uuid4())

    events = s.drain_events()
    # No symptom event from this call — the _checkpoint at the top raised
    # before any check ran.
    assert [e for e in events if e.type == EventType.SYMPTOM] == []


# --- manual flag flip (M4 path preview) ----------------------------------


def test_manual_flag_flip_raises_at_next_checkpoint() -> None:
    """M4 will flip terminate_requested directly from the poll loop. Verify
    that path: no symptom event is needed (manual kill isn't a symptom)."""
    s = _state()
    h = CaspaseCallbackHandler(s)

    # Healthy invocation works.
    h.on_chain_start(serialized={"name": "n"}, inputs={}, run_id=uuid4())
    assert not s.terminate_requested

    # External actor flips the flag — no symptom event.
    s.terminate_requested = True
    s.terminate_reason = "manual kill: deploy"

    with pytest.raises(CaspaseTerminated, match="manual kill"):
        h.on_chain_start(serialized={"name": "n2"}, inputs={}, run_id=uuid4())


# --- no-op path -----------------------------------------------------------


def test_healthy_calls_do_not_flip_or_record_symptoms() -> None:
    """Negative regression: nothing bad happens in the common case."""
    s = _state()  # coding-default
    h = CaspaseCallbackHandler(s)
    for _ in range(3):
        h.on_chain_start(serialized={"name": "n"}, inputs={}, run_id=uuid4())
        h.on_tool_start(serialized={"name": "read_file"}, input_str="x", run_id=uuid4())
    assert not s.terminate_requested
    assert _symptom_events(s) == []


# --- end-to-end with a real LangGraph ------------------------------------


class _GraphState(TypedDict, total=False):
    counter: int


def _looping_graph(state: WatcherState) -> Any:
    """A graph whose node deterministically calls a tool the same way every
    time — designed to trip the loop check after `max_loop_repeats` rounds."""
    handler = CaspaseCallbackHandler(state)

    def loop_node(s: _GraphState) -> _GraphState:
        # Manually fire the LangChain on_tool_start hook to simulate the
        # agent invoking a tool. (In a real agent, langchain would do this
        # via the callback manager. We invoke directly to keep the test
        # self-contained — same code path as production for the M2.3
        # apoptosis wiring.)
        handler.on_tool_start(
            serialized={"name": "read_file"},
            input_str="",
            inputs={"path": "a.txt"},
            run_id=uuid4(),
        )
        return {"counter": s.get("counter", 0) + 1}

    g = StateGraph(_GraphState)
    g.add_node("loop", loop_node)
    g.add_edge(START, "loop")
    # Self-edge: loop forever, until apoptosis raises at the next chain_start.
    g.add_conditional_edges(
        "loop",
        lambda s: "loop" if s.get("counter", 0) < 20 else END,
        {"loop": "loop", END: END},
    )
    compiled = g.compile()
    return with_caspase(compiled, handler)


@pytest.mark.asyncio
async def test_end_to_end_langgraph_dies_cooperatively_on_loop() -> None:
    """The headline M2 test: a runaway agent in a real compiled LangGraph
    self-terminates cooperatively when the loop check trips. No force-kill,
    no graph rewrite — just the cooperative checkpoint mechanism."""
    p = _policy(max_loop_repeats=5, loop_window_actions=20, max_runtime_seconds=120)
    s = _state(p)
    wrapped = _looping_graph(s)

    with pytest.raises(CaspaseTerminated, match="repeated"):
        await wrapped.ainvoke({"counter": 0})

    assert s.terminate_requested
    # Check the symptom landed in the event stream (for death cert).
    symptoms = _symptom_events(s)
    loop_symptoms = [
        e for e in symptoms if e.payload["symptom"] == SymptomType.LOOP.value
    ]
    assert loop_symptoms, "loop symptom should have been recorded"
    assert loop_symptoms[0].payload["severity"] == "terminal"
