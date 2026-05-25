"""Tests for the symptom checks (M2.2).

Coverage:
- Each check returns Healthy below threshold + Terminal at/above
- Loop check counts the most-frequent signature (alternating loops trip it)
- Loop check is bounded by the ring buffer (old repeats age out)
- Cost check fires on EITHER cost OR token cap, with correct trigger label
- Wall-clock check uses monotonic uptime
- Tool scope: empty allowlist = allow-all; non-empty = strict membership
- HEALTHY is a shared singleton (hot-path guarantee)
- run_all skips Healthy, returns multiple Terminals when multiple fire
- Property tests: threshold boundary, monotonicity of the cost trigger
"""

from __future__ import annotations

from collections import deque
from uuid import uuid4

import pytest
from caspase.checks import (
    HEALTHY,
    CheckResult,
    Healthy,
    Terminal,
    Warning,
    check_cost_runaway,
    check_loop,
    check_tool_scope,
    check_wall_clock,
    run_all,
)
from caspase.policies import resolve_policy
from caspase.types import Policy, PolicyThresholds, SymptomType
from caspase.watcher import WatcherState
from hypothesis import given
from hypothesis import strategies as st

# --- helpers --------------------------------------------------------------


def _state(policy: Policy | None = None) -> WatcherState:
    return WatcherState(
        agent_id=uuid4(),
        name="t",
        policy=policy or resolve_policy("coding-default"),
    )


def _policy_with(**threshold_overrides: object) -> Policy:
    """Build a coding-default policy with selected thresholds overridden.

    Lets each test state only what it cares about — keeps the test body
    focused on the boundary it's exercising, not on policy boilerplate.
    """
    base = resolve_policy("coding-default")
    t = base.thresholds.model_dump()
    t.update(threshold_overrides)
    return base.model_copy(update={"thresholds": PolicyThresholds(**t)})


# --- result types ---------------------------------------------------------


def test_healthy_is_shared_singleton() -> None:
    """`is HEALTHY` checks must work — checks are on the hot path and we
    want a cheap shared sentinel, not fresh allocations."""
    p = resolve_policy("coding-default")
    s = _state(p)
    # Empty state, no triggers anywhere → every check should return the
    # same HEALTHY instance.
    assert check_loop(s, p) is HEALTHY
    assert check_cost_runaway(s, p) is HEALTHY
    assert check_wall_clock(s, p) is HEALTHY
    assert check_tool_scope("read_file", p) is HEALTHY


def test_terminal_is_frozen() -> None:
    """Terminal must be immutable — apoptosis logs it, posts it as a symptom
    event, and includes it in the death cert. Mutating mid-flight = bug."""
    t = Terminal(symptom=SymptomType.LOOP, reason="x", detail={"a": 1})
    with pytest.raises((AttributeError, TypeError)):  # frozen dataclass
        t.reason = "y"  # type: ignore[misc]


# --- check_loop -----------------------------------------------------------


def test_loop_healthy_when_buffer_empty() -> None:
    p = _policy_with(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    assert check_loop(s, p) is HEALTHY


def test_loop_healthy_below_threshold() -> None:
    p = _policy_with(max_loop_repeats=5, loop_window_actions=20)
    s = _state(p)
    for _ in range(4):  # 4 identical calls, threshold is 5
        s.record_tool_call("read_file", {"path": "a.txt"})
    assert check_loop(s, p) is HEALTHY


def test_loop_terminal_at_threshold() -> None:
    """count == threshold fires (`>=`, not `>` — the documented contract)."""
    p = _policy_with(max_loop_repeats=5, loop_window_actions=20)
    s = _state(p)
    for _ in range(5):
        s.record_tool_call("read_file", {"path": "a.txt"})
    r = check_loop(s, p)
    assert isinstance(r, Terminal)
    assert r.symptom == SymptomType.LOOP
    assert r.detail["count"] == 5
    assert r.detail["max_loop_repeats"] == 5
    assert "read_file" in r.detail["signature"]


def test_loop_terminal_above_threshold() -> None:
    p = _policy_with(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    for _ in range(7):
        s.record_tool_call("read_file", {"path": "a.txt"})
    r = check_loop(s, p)
    assert isinstance(r, Terminal)
    assert r.detail["count"] == 7


def test_loop_distinct_sigs_dont_trigger() -> None:
    """Tool used many times with different params is not a loop."""
    p = _policy_with(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    for i in range(10):
        s.record_tool_call("read_file", {"path": f"f{i}.txt"})
    assert check_loop(s, p) is HEALTHY


def test_loop_counts_most_common_sig_not_latest() -> None:
    """Alternating between two loops still trips — `most_common` finds the
    worst offender even if it's not the most-recent action."""
    p = _policy_with(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    # 3x A, then 2x B - A should still trip the check.
    s.record_tool_call("read_file", {"path": "a"})
    s.record_tool_call("read_file", {"path": "a"})
    s.record_tool_call("read_file", {"path": "a"})
    s.record_tool_call("write_file", {"path": "b"})
    s.record_tool_call("write_file", {"path": "b"})
    r = check_loop(s, p)
    assert isinstance(r, Terminal)
    assert r.detail["count"] == 3


def test_loop_ring_buffer_ages_out_old_repeats() -> None:
    """Old repeats outside the window must NOT count — this is the whole
    point of having a sliding window. Critical regression test."""
    p = _policy_with(max_loop_repeats=4, loop_window_actions=10)
    s = _state(p)
    # 3 of A (under threshold), then 10 of B — A's repeats get evicted by
    # B's, so neither should fire.
    for _ in range(3):
        s.record_tool_call("read_file", {"path": "a"})
    for _ in range(7):
        s.record_tool_call("write_file", {"path": "b"})
    # Window now: 3 A + 7 B = 10. A=3 (below 4), B=7 (above 4) → Terminal.
    r = check_loop(s, p)
    assert isinstance(r, Terminal)
    assert r.detail["count"] == 7  # B, not A
    # Now push enough Bs to fully evict the As.
    for _ in range(3):
        s.record_tool_call("write_file", {"path": "b"})
    # Window: 10 B, no A. Still Terminal but only B is counted.
    r2 = check_loop(s, p)
    assert isinstance(r2, Terminal)
    assert r2.detail["count"] == 10
    assert r2.detail["window_size"] == 10


def test_loop_window_size_in_detail_reflects_actual_buffer_len() -> None:
    """`window_size` in detail = current deque len, not policy maxlen.
    Helps operators distinguish 'just started, already looping' from
    'looped for a long time'."""
    p = _policy_with(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    for _ in range(3):
        s.record_tool_call("read_file", {"path": "a"})
    r = check_loop(s, p)
    assert isinstance(r, Terminal)
    assert r.detail["window_size"] == 3


# --- check_cost_runaway --------------------------------------------------


def test_cost_healthy_at_zero() -> None:
    p = _policy_with(max_cost_usd=10.0, max_tokens_per_run=1000)
    s = _state(p)
    assert check_cost_runaway(s, p) is HEALTHY


def test_cost_terminal_when_cost_cap_hit() -> None:
    p = _policy_with(max_cost_usd=1.0, max_tokens_per_run=10_000_000)
    s = _state(p)
    s.total_cost_usd = 1.0  # exactly at cap → fires (>=)
    r = check_cost_runaway(s, p)
    assert isinstance(r, Terminal)
    assert r.symptom == SymptomType.TOKEN_RUNAWAY
    assert r.detail["trigger"] == "cost"
    assert r.detail["cap_usd"] == 1.0


def test_cost_terminal_when_token_cap_hit() -> None:
    """Token cap is the fallback when pricing returns $0 (unknown model)."""
    p = _policy_with(max_cost_usd=1_000_000.0, max_tokens_per_run=1000)
    s = _state(p)
    s.total_input_tokens = 600
    s.total_output_tokens = 400  # total = 1000 → fires
    r = check_cost_runaway(s, p)
    assert isinstance(r, Terminal)
    assert r.detail["trigger"] == "tokens"
    assert r.detail["total_tokens"] == 1000


def test_cost_check_prefers_cost_trigger_when_both_fire() -> None:
    """When both limits are blown, the cost trigger is reported — that's the
    canonical signal an operator cares about."""
    p = _policy_with(max_cost_usd=1.0, max_tokens_per_run=10)
    s = _state(p)
    s.total_cost_usd = 5.0
    s.total_input_tokens = 100
    r = check_cost_runaway(s, p)
    assert isinstance(r, Terminal)
    assert r.detail["trigger"] == "cost"


def test_cost_healthy_just_below_both_caps() -> None:
    p = _policy_with(max_cost_usd=10.0, max_tokens_per_run=1000)
    s = _state(p)
    s.total_cost_usd = 9.99
    s.total_input_tokens = 500
    s.total_output_tokens = 499  # 999 < 1000
    assert check_cost_runaway(s, p) is HEALTHY


# --- check_wall_clock ----------------------------------------------------


def test_wall_clock_healthy_for_fresh_state() -> None:
    p = _policy_with(max_runtime_seconds=3600)
    s = _state(p)
    assert check_wall_clock(s, p) is HEALTHY


def test_wall_clock_terminal_when_uptime_exceeds_cap() -> None:
    p = _policy_with(max_runtime_seconds=10)
    s = _state(p)
    # Backdate `started_monotonic` to simulate a long-running agent.
    import time

    s.started_monotonic = time.monotonic() - 100  # 100s elapsed
    r = check_wall_clock(s, p)
    assert isinstance(r, Terminal)
    assert r.symptom == SymptomType.WALL_CLOCK
    assert r.detail["runtime_seconds"] >= 100
    assert r.detail["cap_seconds"] == 10


def test_wall_clock_uses_monotonic_not_wall_clock() -> None:
    """Even if the system clock jumps backwards, the check should still fire
    correctly because uptime is derived from `time.monotonic`."""
    p = _policy_with(max_runtime_seconds=1)
    s = _state(p)
    import time

    s.started_monotonic = time.monotonic() - 5  # 5s elapsed via monotonic
    # Even if we mess with the wall-clock attribute, monotonic-based uptime
    # still drives the verdict.
    from datetime import UTC, datetime, timedelta

    s.started_at = datetime.now(UTC) + timedelta(seconds=999)  # absurd future
    r = check_wall_clock(s, p)
    assert isinstance(r, Terminal)


# --- check_tool_scope ----------------------------------------------------


def test_tool_scope_empty_allowlist_allows_anything() -> None:
    """The permissive policy uses an empty allowlist to mean 'any tool'."""
    p = resolve_policy("permissive")
    assert p.tool_allowlist == []
    assert check_tool_scope("anything", p) is HEALTHY
    assert check_tool_scope("rm -rf /", p) is HEALTHY


def test_tool_scope_terminal_when_not_in_allowlist() -> None:
    p = resolve_policy("strict")  # ["read_file", "search"]
    r = check_tool_scope("write_file", p)
    assert isinstance(r, Terminal)
    assert r.symptom == SymptomType.TOOL_SCOPE_VIOLATION
    assert r.detail["tool"] == "write_file"
    assert "read_file" in r.detail["allowlist"]


def test_tool_scope_healthy_when_in_allowlist() -> None:
    p = resolve_policy("strict")
    assert check_tool_scope("read_file", p) is HEALTHY
    assert check_tool_scope("search", p) is HEALTHY


def test_tool_scope_case_sensitive() -> None:
    """Tool names are case-sensitive — exact-match check, no normalization."""
    p = resolve_policy("strict")
    r = check_tool_scope("READ_FILE", p)
    assert isinstance(r, Terminal)


# --- run_all -------------------------------------------------------------


def test_run_all_empty_when_all_healthy() -> None:
    p = resolve_policy("coding-default")
    s = _state(p)
    assert run_all(s, p) == []


def test_run_all_returns_single_terminal_when_one_fires() -> None:
    p = _policy_with(max_cost_usd=1.0)
    s = _state(p)
    s.total_cost_usd = 5.0
    results = run_all(s, p)
    assert len(results) == 1
    assert isinstance(results[0], Terminal)
    assert results[0].symptom == SymptomType.TOKEN_RUNAWAY


def test_run_all_returns_multiple_terminals_when_multiple_fire() -> None:
    """The plan says symptoms are logged as they happen; the *first* Terminal
    triggers apoptosis. run_all returning all of them lets M2.3 log them
    all but act on the first."""
    p = _policy_with(
        max_loop_repeats=3,
        loop_window_actions=20,
        max_cost_usd=1.0,
        max_runtime_seconds=1,
    )
    s = _state(p)
    # Trip loop
    for _ in range(3):
        s.record_tool_call("x", {})
    # Trip cost
    s.total_cost_usd = 100.0
    # Trip wall clock
    import time

    s.started_monotonic = time.monotonic() - 60
    results = run_all(s, p)
    assert len(results) == 3
    symptoms = {r.symptom for r in results}
    assert symptoms == {
        SymptomType.LOOP,
        SymptomType.TOKEN_RUNAWAY,
        SymptomType.WALL_CLOCK,
    }


def test_run_all_excludes_tool_scope() -> None:
    """`check_tool_scope` is called separately from `on_tool_start`; run_all
    must never invoke it (it has no tool name to pass)."""
    # Use the strict policy whose allowlist would reject most tools — but
    # since we never call check_tool_scope here, run_all returns [] even
    # though *if* it had a tool name, scope would fire.
    p = resolve_policy("strict")
    s = _state(p)
    assert run_all(s, p) == []


def test_run_all_return_type_excludes_healthy() -> None:
    """Type narrowing: callers should be able to iterate run_all() and treat
    every element as actionable (Terminal or Warning), not filter for None."""
    p = _policy_with(max_cost_usd=0.01)
    s = _state(p)
    s.total_cost_usd = 1.0
    results = run_all(s, p)
    for r in results:
        assert isinstance(r, Terminal | Warning)
        assert not isinstance(r, Healthy)


# --- property tests ------------------------------------------------------


@given(
    threshold=st.integers(min_value=1, max_value=50),
    count=st.integers(min_value=0, max_value=100),
)
def test_property_loop_threshold_boundary(threshold: int, count: int) -> None:
    """For any (threshold, count): Terminal iff count >= threshold (and
    count <= window so the buffer can hold it)."""
    # Make the window comfortably large so eviction never confounds the test.
    window = max(threshold, count, 1) + 5
    p = _policy_with(max_loop_repeats=threshold, loop_window_actions=window)
    s = _state(p)
    for _ in range(count):
        s.record_tool_call("x", {})
    r = check_loop(s, p)
    if count >= threshold:
        assert isinstance(r, Terminal), f"expected Terminal at count={count} threshold={threshold}"
        assert r.detail["count"] == count
    else:
        assert r is HEALTHY, f"expected HEALTHY at count={count} threshold={threshold}"


@given(
    cap=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
    cost=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
)
def test_property_cost_threshold_boundary(cap: float, cost: float) -> None:
    """For any (cap, cost): Terminal iff cost >= cap (tokens held at 0 to
    isolate the cost trigger)."""
    p = _policy_with(max_cost_usd=cap, max_tokens_per_run=10**9)
    s = _state(p)
    s.total_cost_usd = cost
    r = check_cost_runaway(s, p)
    if cost >= cap:
        assert isinstance(r, Terminal)
        assert r.detail["trigger"] == "cost"
    else:
        assert r is HEALTHY


@given(
    cap=st.integers(min_value=1, max_value=10_000_000),
    input_tokens=st.integers(min_value=0, max_value=20_000_000),
    output_tokens=st.integers(min_value=0, max_value=20_000_000),
)
def test_property_token_threshold_boundary(
    cap: int, input_tokens: int, output_tokens: int
) -> None:
    """For any (cap, input, output): Terminal iff input+output >= cap when
    cost stays at 0."""
    p = _policy_with(max_cost_usd=1_000_000.0, max_tokens_per_run=cap)
    s = _state(p)
    s.total_input_tokens = input_tokens
    s.total_output_tokens = output_tokens
    r = check_cost_runaway(s, p)
    if input_tokens + output_tokens >= cap:
        assert isinstance(r, Terminal)
        assert r.detail["trigger"] == "tokens"
    else:
        assert r is HEALTHY


@given(
    allowlist=st.lists(
        st.text(min_size=1, max_size=20).filter(lambda s: s.strip() == s),
        min_size=0,
        max_size=8,
        unique=True,
    ),
    tool=st.text(min_size=1, max_size=20).filter(lambda s: s.strip() == s),
)
def test_property_tool_scope_empty_is_allow_all(allowlist: list[str], tool: str) -> None:
    """Empty allowlist → always Healthy. Non-empty → Healthy iff member."""
    # Build a fresh policy each time — coding-default has a fixed allowlist
    # we don't want bleeding in.
    p = Policy(name="prop", tool_allowlist=allowlist)
    r = check_tool_scope(tool, p)
    if not allowlist or tool in allowlist:
        assert r is HEALTHY
    else:
        assert isinstance(r, Terminal)
        assert r.symptom == SymptomType.TOOL_SCOPE_VIOLATION


# --- regression: ring-buffer + loop interaction --------------------------


def test_loop_check_doesnt_mutate_buffer() -> None:
    """check_loop must be a pure read — never mutate the deque it inspects."""
    p = _policy_with(max_loop_repeats=3, loop_window_actions=20)
    s = _state(p)
    for i in range(5):
        s.record_tool_call("x", {"i": i})
    before = list(s.loop_signatures)
    check_loop(s, p)
    after = list(s.loop_signatures)
    assert before == after


def test_loop_uses_state_deque_not_a_copy() -> None:
    """Catching a refactor risk: if someone wraps `loop_signatures` in a
    list inside check_loop, it could decouple from the live ring buffer.
    The check must see exactly what record_tool_call has written."""
    p = _policy_with(max_loop_repeats=3, loop_window_actions=5)
    s = _state(p)
    # Underlying deque maxlen is 5; verify after writing 8 entries only the
    # last 5 are seen by check_loop.
    for i in range(8):
        s.record_tool_call("x", {"i": i})  # 8 distinct sigs
    assert len(s.loop_signatures) == 5  # evicted to maxlen
    # With 5 distinct sigs and threshold 3, no repeats → Healthy.
    assert check_loop(s, p) is HEALTHY


# --- exported surface ----------------------------------------------------


def test_check_result_union_is_exported() -> None:
    """Callers downstream (M2.3 apoptosis wiring) need to type their handlers
    over `CheckResult`. Make sure the alias actually exists at import time."""
    assert CheckResult is not None
    # Smoke: union membership
    sample: CheckResult = HEALTHY
    assert isinstance(sample, Healthy)


def test_deque_default_keeps_maxlen_after_post_init() -> None:
    """Companion regression for M2.1 wiring: WatcherState.__post_init__ must
    have applied the policy's window to the deque. If this breaks, every
    loop check becomes unbounded."""
    p = _policy_with(loop_window_actions=7)
    s = _state(p)
    assert isinstance(s.loop_signatures, deque)
    assert s.loop_signatures.maxlen == 7
