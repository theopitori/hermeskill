"""Built-in supervision policies.

Three defaults ship with the SDK and are selected by name — set
`HERMESKILL_POLICY` (or `policy=` in `~/.hermeskill/config.toml`):

    strict           # least permissive — tight caps and allowlist
    coding-default   # everyday coding agents
    permissive       # loose, for exploration

Custom policies (load-from-YAML / server-side CRUD) land in M5 alongside
the grant system. For now, the SDK is the authoritative source of policy
*contents* — the server only stores the name on the agent row.

Tuning rationale, from looser to stricter (numbers are the deliberate
opinionated defaults, not the only valid configuration):

  * **strict** — tight wall-clock + cost ceilings, narrow tool surface, no
    grantable symptoms. For untrusted code paths or experimental agents.
  * **coding-default** — what the spec ships for general coding agents.
    Matches the YAML in the plan verbatim.
  * **permissive** — generous limits, broad tool surface, scope violations
    grantable. For trusted internal agents under operator supervision.
"""

from __future__ import annotations

from hermeskill.exceptions import HermeskillError
from hermeskill.types import (
    ApoptosisProofingDefaults,
    Policy,
    PolicyThresholds,
    SymptomType,
)


class UnknownPolicyError(HermeskillError):
    """Raised by `resolve_policy()` when the requested name isn't shipped."""


STRICT = Policy(
    name="strict",
    thresholds=PolicyThresholds(
        max_loop_repeats=3,
        loop_window_actions=15,
        max_tokens_per_run=100_000,
        max_cost_usd=2.0,
        max_runtime_seconds=300,
        heartbeat_interval_seconds=15,
        cooperative_grace_seconds=5,
        verification_timeout_seconds=20,
    ),
    tool_allowlist=["read_file", "search"],
    apoptosis_proofing=ApoptosisProofingDefaults(
        allowed_symptoms=[],
        max_duration_hours=1,
    ),
)


CODING_DEFAULT = Policy(
    name="coding-default",
    thresholds=PolicyThresholds(
        max_loop_repeats=5,
        loop_window_actions=20,
        max_tokens_per_run=500_000,
        max_cost_usd=25.0,
        max_runtime_seconds=1800,
        heartbeat_interval_seconds=30,
        cooperative_grace_seconds=10,
        verification_timeout_seconds=30,
    ),
    tool_allowlist=["read_file", "write_file", "run_bash", "search", "http_get"],
    apoptosis_proofing=ApoptosisProofingDefaults(
        allowed_symptoms=[SymptomType.TOOL_SCOPE_VIOLATION],
        max_duration_hours=4,
    ),
)


PERMISSIVE = Policy(
    name="permissive",
    thresholds=PolicyThresholds(
        max_loop_repeats=10,
        loop_window_actions=40,
        max_tokens_per_run=2_000_000,
        max_cost_usd=100.0,
        max_runtime_seconds=7200,
        heartbeat_interval_seconds=60,
        cooperative_grace_seconds=15,
        verification_timeout_seconds=60,
    ),
    tool_allowlist=[],  # empty == "any tool"; checks.py treats this as opt-out
    apoptosis_proofing=ApoptosisProofingDefaults(
        allowed_symptoms=[
            SymptomType.TOOL_SCOPE_VIOLATION,
            SymptomType.LOOP,
        ],
        max_duration_hours=8,
    ),
)


DEFAULT_POLICIES: dict[str, Policy] = {
    STRICT.name: STRICT,
    CODING_DEFAULT.name: CODING_DEFAULT,
    PERMISSIVE.name: PERMISSIVE,
}


def resolve_policy(name: str) -> Policy:
    """Return the shipped Policy with this name, or raise `UnknownPolicyError`.

    Returns a *copy* so accidental mutation of the returned object can't
    contaminate other watchers in the same process — the constants above
    are intended to be effectively immutable.
    """
    try:
        original = DEFAULT_POLICIES[name]
    except KeyError as exc:
        known = ", ".join(sorted(DEFAULT_POLICIES))
        raise UnknownPolicyError(
            f"unknown policy: {name!r}; known policies are: {known}"
        ) from exc
    return original.model_copy(deep=True)


def list_policy_names() -> list[str]:
    """Sorted list of shipped policy names. Useful for CLI help / error messages."""
    return sorted(DEFAULT_POLICIES)
