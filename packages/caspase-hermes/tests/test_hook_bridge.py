"""Unit tests for caspase_hermes.bridge.

Each test asserts that a Hermes hook event produces the correct WatcherState
mutation. The bridge has no I/O and no Hermes dependency — everything is
pure WatcherState.

Coverage:
- on_pre_tool_call: records tool call + loop ring buffer
- on_pre_tool_call: loop Terminal fires on repeat signatures
- on_pre_tool_call: cost Terminal fires when cap exceeded
- on_pre_tool_call: tool-scope Terminal fires for disallowed tool
- on_pre_tool_call: Terminal demoted to Warning when grant applies (M5)
- on_post_tool_call: records lifecycle + re-runs checks
- on_pre_llm_call: records lifecycle event
- on_post_llm_call: updates token/cost counters
- on_post_llm_call: cost Terminal fires after token accumulation
- on_session_end: records lifecycle + shutdown step
- First-cause wins across multiple verdicts
"""

from __future__ import annotations

from caspase.checks import Terminal
from caspase.types import EventType, SymptomType
from caspase.watcher import WatcherState
from caspase_hermes.bridge import (
    on_post_llm_call,
    on_post_tool_call,
    on_pre_llm_call,
    on_pre_tool_call,
    on_session_end,
)

from tests.conftest import make_policy, make_state

# --- on_pre_tool_call --------------------------------------------------------


def test_pre_tool_call_records_tool_event(state: WatcherState) -> None:
    on_pre_tool_call(state, "read_file", {"path": "/tmp/foo"})
    events = state.drain_events()
    tool_events = [e for e in events if e.type == EventType.TOOL_CALL]
    assert len(tool_events) == 1
    assert tool_events[0].payload["tool"] == "read_file"


def test_pre_tool_call_populates_loop_buffer(state: WatcherState) -> None:
    on_pre_tool_call(state, "read_file", {"path": "/tmp/foo"})
    assert len(state.loop_signatures) == 1


def test_pre_tool_call_loop_terminal_fires(state: WatcherState) -> None:
    policy = make_policy(max_loop_repeats=3, loop_window_actions=10)
    s = make_state(policy)
    inputs = {"path": "/tmp/loop"}
    for _ in range(3):
        on_pre_tool_call(s, "read_file", inputs)
    assert s.terminate_requested
    assert s.terminate_reason is not None
    assert "LOOP" in s.terminate_reason.upper() or "loop" in s.terminate_reason.lower() or "repeated" in s.terminate_reason


def test_pre_tool_call_cost_terminal_fires() -> None:
    policy = make_policy(max_cost_usd=0.00001)
    s = make_state(policy)
    s.total_cost_usd = 1.0  # already over cap
    verdicts = on_pre_tool_call(s, "read_file", {})
    assert any(isinstance(v, Terminal) and v.symptom == SymptomType.TOKEN_RUNAWAY for v in verdicts)
    assert s.terminate_requested


def test_pre_tool_call_scope_violation_fires() -> None:
    policy = make_policy()
    policy = policy.model_copy(update={"tool_allowlist": ["read_file"]})
    s = make_state(policy)
    verdicts = on_pre_tool_call(s, "delete_everything", {})
    assert any(isinstance(v, Terminal) and v.symptom == SymptomType.TOOL_SCOPE_VIOLATION for v in verdicts)
    assert s.terminate_requested


def test_pre_tool_call_grant_demotes_terminal() -> None:
    from uuid import uuid4 as _uuid4
    policy = make_policy(max_loop_repeats=3, loop_window_actions=10)
    s = make_state(policy)
    grant_id = str(_uuid4())
    s.grants = [{"id": grant_id, "symptoms": ["loop"], "expires_at": "2099-01-01T00:00:00+00:00", "reason": "test grant"}]
    inputs = {"path": "/tmp/loop"}
    for _ in range(3):
        on_pre_tool_call(s, "read_file", inputs)
    # Grant should suppress the loop Terminal → Warning; agent stays alive
    assert not s.terminate_requested


def test_pre_tool_call_no_false_terminal(state: WatcherState) -> None:
    verdicts = on_pre_tool_call(state, "read_file", {"path": "/tmp/a"})
    assert verdicts == []
    assert not state.terminate_requested


# --- on_post_tool_call -------------------------------------------------------


def test_post_tool_call_records_lifecycle(state: WatcherState) -> None:
    on_post_tool_call(state, "read_file", {}, "output")
    events = state.drain_events()
    lifecycle = [e for e in events if e.type == EventType.LIFECYCLE]
    assert any(e.payload.get("phase") == "tool_end" for e in lifecycle)


def test_post_tool_call_does_not_fire_on_clean_state(state: WatcherState) -> None:
    on_post_tool_call(state, "read_file", {}, "output")
    assert not state.terminate_requested


# --- on_pre_llm_call ---------------------------------------------------------


def test_pre_llm_call_records_lifecycle(state: WatcherState) -> None:
    on_pre_llm_call(state, "claude-opus-4-7", [{"role": "user", "content": "hi"}])
    events = state.drain_events()
    lifecycle = [e for e in events if e.type == EventType.LIFECYCLE]
    assert any(e.payload.get("phase") == "llm_start" for e in lifecycle)


# --- on_post_llm_call --------------------------------------------------------


def test_post_llm_call_updates_token_counters(state: WatcherState) -> None:
    on_post_llm_call(state, "claude-opus-4-7", 100, 50)
    assert state.total_input_tokens == 100
    assert state.total_output_tokens == 50


def test_post_llm_call_records_llm_event(state: WatcherState) -> None:
    on_post_llm_call(state, "claude-opus-4-7", 100, 50)
    events = state.drain_events()
    llm_events = [e for e in events if e.type == EventType.LLM_CALL]
    assert len(llm_events) == 1
    assert llm_events[0].payload["model"] == "claude-opus-4-7"
    assert llm_events[0].payload["input_tokens"] == 100


def test_post_llm_call_cost_terminal_fires() -> None:
    policy = make_policy(max_cost_usd=0.00001)
    s = make_state(policy)
    on_post_llm_call(s, "claude-opus-4-7", 1_000_000, 1_000_000)
    assert s.terminate_requested


# --- on_session_end ----------------------------------------------------------


def test_session_end_records_lifecycle(state: WatcherState) -> None:
    on_session_end(state)
    events = state.drain_events()
    lifecycle = [e for e in events if e.type == EventType.LIFECYCLE]
    assert any(e.payload.get("phase") == "session_end" for e in lifecycle)


def test_session_end_records_shutdown_step(state: WatcherState) -> None:
    on_session_end(state)
    assert any(s["step"] == "hermes_session_ended" for s in state.shutdown_log)


# --- first-cause-wins --------------------------------------------------------


def test_first_cause_wins_across_verdicts() -> None:
    policy = make_policy(max_loop_repeats=3, loop_window_actions=10, max_cost_usd=0.0)
    s = make_state(policy)
    # Cost cap already exceeded
    s.total_cost_usd = 1.0
    # Trigger loop too
    inputs = {"path": "/tmp/x"}
    for _ in range(3):
        on_pre_tool_call(s, "read_file", inputs)
    first_reason = s.terminate_reason
    assert first_reason is not None
    # Subsequent calls don't overwrite first reason
    on_pre_tool_call(s, "other_tool", {"x": 1})
    assert s.terminate_reason == first_reason
