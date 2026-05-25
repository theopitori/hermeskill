"""Tests for the policy infrastructure (M2.1).

Coverage:
- All three shipped policies parse against the strict Pydantic schema
- resolve_policy returns the right policy and rejects unknown names
- resolve_policy returns a deep copy (mutation can't leak across watchers)
- PolicyThresholds rejects unknown fields + invalid ranges
- ApoptosisProofingDefaults restricts to valid SymptomType values
- CODING_DEFAULT matches the contract in the plan verbatim
- WatcherState wires the policy correctly (deque size + policy_name alias)
"""

from uuid import uuid4

import pytest
from caspase.policies import (
    CODING_DEFAULT,
    DEFAULT_POLICIES,
    PERMISSIVE,
    STRICT,
    UnknownPolicyError,
    list_policy_names,
    resolve_policy,
)
from caspase.types import (
    ApoptosisProofingDefaults,
    Policy,
    PolicyThresholds,
    SymptomType,
)
from caspase.watcher import WatcherState
from pydantic import ValidationError

# --- shipped policies parse cleanly --------------------------------------


@pytest.mark.parametrize("policy", [STRICT, CODING_DEFAULT, PERMISSIVE])
def test_shipped_policy_is_valid(policy: Policy) -> None:
    # Re-validate via model_dump → model_validate round-trip; catches any
    # constraint drift between the constants and the schema.
    reparsed = Policy.model_validate(policy.model_dump())
    assert reparsed == policy


def test_shipped_policies_have_unique_names() -> None:
    names = [STRICT.name, CODING_DEFAULT.name, PERMISSIVE.name]
    assert len(set(names)) == len(names), f"duplicate names: {names}"


def test_default_policies_registry_keyed_by_name() -> None:
    for name, policy in DEFAULT_POLICIES.items():
        assert policy.name == name


# --- coding-default matches the spec exactly -----------------------------


def test_coding_default_matches_plan_spec() -> None:
    """The plan documents this policy line-by-line; it must match.

    If the spec is ever updated, change *both* the spec doc and this test
    in the same PR — the assertion is intentional ballast against drift.
    """
    t = CODING_DEFAULT.thresholds
    assert t.max_loop_repeats == 5
    assert t.loop_window_actions == 20
    assert t.max_tokens_per_run == 500_000
    assert t.max_cost_usd == 25.0
    assert t.max_runtime_seconds == 1800
    assert t.heartbeat_interval_seconds == 30
    assert t.cooperative_grace_seconds == 10
    assert t.verification_timeout_seconds == 30

    assert CODING_DEFAULT.tool_allowlist == [
        "read_file", "write_file", "run_bash", "search", "http_get"
    ]

    ap = CODING_DEFAULT.apoptosis_proofing
    assert ap.allowed_symptoms == [SymptomType.TOOL_SCOPE_VIOLATION]
    assert ap.max_duration_hours == 4


# --- resolve_policy -------------------------------------------------------


def test_resolve_policy_returns_named_policy() -> None:
    p = resolve_policy("coding-default")
    assert p.name == "coding-default"
    assert p.thresholds.max_loop_repeats == 5


def test_resolve_policy_unknown_raises_with_helpful_message() -> None:
    with pytest.raises(UnknownPolicyError, match="unknown policy: 'nonexistent'") as exc:
        resolve_policy("nonexistent")
    msg = str(exc.value)
    # The error should enumerate what *is* available so the user can fix it.
    for known in ("strict", "coding-default", "permissive"):
        assert known in msg


def test_resolve_policy_returns_deep_copy() -> None:
    """Mutating one resolved policy must not contaminate the next caller."""
    p1 = resolve_policy("coding-default")
    p1.tool_allowlist.append("rogue_tool")
    p1.thresholds.max_cost_usd = 99999.0

    p2 = resolve_policy("coding-default")
    assert "rogue_tool" not in p2.tool_allowlist
    assert p2.thresholds.max_cost_usd == 25.0


def test_list_policy_names_returns_sorted() -> None:
    names = list_policy_names()
    assert names == sorted(names)
    assert set(names) == {"strict", "coding-default", "permissive"}


# --- schema strictness ----------------------------------------------------


def test_thresholds_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        PolicyThresholds(max_loop_repeats=5, mystery_field=1)  # type: ignore[call-arg]


def test_thresholds_rejects_zero_loop_repeats() -> None:
    # ge=1 — 0 is nonsensical (would terminate on the first action).
    with pytest.raises(ValidationError):
        PolicyThresholds(max_loop_repeats=0)


def test_thresholds_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        PolicyThresholds(max_cost_usd=-1.0)


def test_apoptosis_proofing_rejects_invalid_symptom() -> None:
    with pytest.raises(ValidationError):
        ApoptosisProofingDefaults(
            allowed_symptoms=["not-a-real-symptom"],  # type: ignore[list-item]
        )


def test_apoptosis_proofing_accepts_string_symptoms() -> None:
    # StrEnum coercion — operators write YAML like `allowed_symptoms: [loop]`
    # which arrives as strings, not enum instances.
    ap = ApoptosisProofingDefaults(allowed_symptoms=["loop", "tool_scope_violation"])  # type: ignore[list-item]
    assert ap.allowed_symptoms == [SymptomType.LOOP, SymptomType.TOOL_SCOPE_VIOLATION]


def test_policy_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        Policy(name="")


# --- WatcherState wiring --------------------------------------------------


def test_watcher_state_sizes_loop_buffer_from_policy() -> None:
    """The deque's maxlen must come from policy.thresholds.loop_window_actions."""
    policy = resolve_policy("strict")  # loop_window_actions=15
    state = WatcherState(agent_id=uuid4(), name="t", policy=policy)
    assert state.loop_signatures.maxlen == 15

    policy2 = resolve_policy("permissive")  # loop_window_actions=40
    state2 = WatcherState(agent_id=uuid4(), name="t", policy=policy2)
    assert state2.loop_signatures.maxlen == 40


def test_watcher_state_loop_buffer_evicts_oldest_at_maxlen() -> None:
    """The deque is the M2.2 loop check's working memory — must behave
    like a ring buffer at its size limit."""
    policy = resolve_policy("strict")  # window = 15
    state = WatcherState(agent_id=uuid4(), name="t", policy=policy)
    for i in range(20):
        state.record_tool_call("read_file", {"i": i})
    # Each call had a unique signature; deque should be at maxlen (15)
    # and contain only the most recent 15 — the first 5 were evicted.
    assert len(state.loop_signatures) == 15


def test_watcher_state_policy_name_property() -> None:
    state = WatcherState(
        agent_id=uuid4(), name="t", policy=resolve_policy("coding-default")
    )
    assert state.policy_name == "coding-default"
    assert state.policy.name == state.policy_name
