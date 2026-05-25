"""Tests for the LangChain callback handler.

We construct real LangChain types where possible (LLMResult, ChatGeneration,
AIMessage) so the token-extraction logic is exercised against the real shape.
"""

from uuid import uuid4

from caspase.langchain import CaspaseCallbackHandler, _extract_token_usage
from caspase.policies import resolve_policy
from caspase.types import EventType
from caspase.watcher import WatcherState
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult


def _state() -> WatcherState:
    return WatcherState(agent_id=uuid4(), name="t", policy=resolve_policy("coding-default"))


# --- tool callbacks -------------------------------------------------------


def test_on_tool_start_records_tool_call() -> None:
    s = _state()
    h = CaspaseCallbackHandler(s)
    h.on_tool_start(
        serialized={"name": "read_file"},
        input_str="",
        inputs={"path": "a.txt"},
        run_id=uuid4(),
    )
    events = s.drain_events()
    assert len(events) == 1
    assert events[0].type == EventType.TOOL_CALL
    assert events[0].payload["tool"] == "read_file"
    # Loop signature must have been updated too
    assert len(s.loop_signatures) == 1


def test_on_tool_start_handles_missing_name() -> None:
    # Use permissive policy (empty allowlist = allow any) so the tool-scope
    # check doesn't fire on the "<unknown>" fallback name. This test is
    # about handler robustness to missing metadata, not scope semantics.
    s = WatcherState(agent_id=uuid4(), name="t", policy=resolve_policy("permissive"))
    h = CaspaseCallbackHandler(s)
    h.on_tool_start(serialized={}, input_str="x", run_id=uuid4())
    events = s.drain_events()
    tool_events = [e for e in events if e.type == EventType.TOOL_CALL]
    assert tool_events[0].payload["tool"] == "<unknown>"


def test_on_tool_error_records_lifecycle() -> None:
    s = _state()
    h = CaspaseCallbackHandler(s)
    h.on_tool_error(error=RuntimeError("boom"), run_id=uuid4())
    events = s.drain_events()
    assert len(events) == 1
    assert events[0].type == EventType.LIFECYCLE
    assert events[0].payload["phase"] == "tool_error"
    assert "RuntimeError" in events[0].payload["error"]


# --- LLM callbacks --------------------------------------------------------


def test_on_llm_end_extracts_modern_usage_metadata() -> None:
    s = _state()
    h = CaspaseCallbackHandler(s)
    msg = AIMessage(
        content="hello",
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        response_metadata={"model_name": "claude-haiku-4-5"},
    )
    response = LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={})
    h.on_llm_end(response, run_id=uuid4())

    assert s.total_input_tokens == 100
    assert s.total_output_tokens == 50
    assert s.total_cost_usd > 0  # priced model → non-zero cost
    events = s.drain_events()
    assert len(events) == 1
    assert events[0].type == EventType.LLM_CALL
    assert events[0].payload["model"] == "claude-haiku-4-5"


def test_on_llm_end_legacy_usage_shape() -> None:
    """OpenAI legacy llm_output.token_usage path."""
    s = _state()
    h = CaspaseCallbackHandler(s)
    msg = AIMessage(content="x")
    response = LLMResult(
        generations=[[ChatGeneration(message=msg)]],
        llm_output={
            "model_name": "gpt-4o-mini",
            "token_usage": {"prompt_tokens": 200, "completion_tokens": 100},
        },
    )
    h.on_llm_end(response, run_id=uuid4())
    assert s.total_input_tokens == 200
    assert s.total_output_tokens == 100


def test_on_llm_end_unknown_shape_defaults_to_zero() -> None:
    s = _state()
    h = CaspaseCallbackHandler(s)
    response = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="x"))]],
        llm_output=None,
    )
    h.on_llm_end(response, run_id=uuid4())
    # Should not crash; counters stay at 0
    assert s.total_input_tokens == 0
    assert s.total_output_tokens == 0
    # Event still recorded so we know the call happened
    assert len(s.drain_events()) == 1


# --- _extract_token_usage helper -----------------------------------------


def test_extract_token_usage_handles_anthropic_input_output_keys() -> None:
    response = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="x"))]],
        llm_output={
            "model": "claude-sonnet-4-7",
            "usage": {"input_tokens": 42, "output_tokens": 7},
        },
    )
    model, inp, out = _extract_token_usage(response)
    assert model == "claude-sonnet-4-7"
    assert inp == 42
    assert out == 7


# --- chain lifecycle -----------------------------------------------------


def test_on_chain_start_only_records_top_level() -> None:
    s = _state()
    h = CaspaseCallbackHandler(s)
    # top-level start (no parent) → recorded
    h.on_chain_start(serialized={"name": "main"}, inputs={}, run_id=uuid4())
    # child start (has parent) → ignored to reduce noise
    h.on_chain_start(
        serialized={"name": "subchain"},
        inputs={},
        run_id=uuid4(),
        parent_run_id=uuid4(),
    )
    events = s.drain_events()
    assert len(events) == 1
    assert events[0].payload["chain"] == "main"
