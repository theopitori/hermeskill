"""Symptom checks — the deterministic, sub-millisecond "should this agent die?"
predicates that run in-process on every callback boundary.

Each check is a **pure function** over `(WatcherState, Policy)` (or, for
`check_tool_scope`, `(tool_name, Policy)`) returning one of three verdicts:

    * `Healthy`  — nothing to report
    * `Warning`  — symptom fired but a grant demoted it (M5; no M2 check
      produces this yet — the type is reserved so the M5 grant-application
      wrapper can return it from a function with the same signature)
    * `Terminal` — flip the apoptosis flag

The orchestrator `run_all(state, policy)` runs the state-only checks and
returns every non-Healthy verdict. The first `Terminal` is what M2.3's
apoptosis wiring acts on; warnings are logged. **`check_tool_scope` is
parameterized by the inbound tool name** so it doesn't fit `run_all`; the
LangChain handler calls it directly from `on_tool_start` (M2.3).

The five MVP symptoms from the plan:

  1. **loop** — `check_loop` (this file)
  2. **token/cost runaway** — `check_cost_runaway` (this file)
  3. **wall-clock runaway** — `check_wall_clock` (this file)
  4. **tool scope violation** — `check_tool_scope` (this file)
  5. **heartbeat stale** — server-side; lives in `control_plane.domain.kill_engine`
  6. **manual kill pending** — not a check; the poll loop (M4) flips
     `state.terminate_requested` directly. The apoptosis path doesn't need
     a predicate for it — the flag is the signal.

Determinism guarantee: given the same `(state, policy)` snapshot, every
check returns the same verdict. No randomness, no I/O, no time queries
except `state.uptime_seconds()` (the one unavoidable wall-clock read for
runtime cap enforcement). This is what makes them safe to property-test.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

from stasis_agent.types import Policy, SymptomType
from stasis_agent.watcher import WatcherState

# --- result types ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Healthy:
    """Nothing to report. Returned via the `HEALTHY` singleton — checks are
    on the hot path and we want this to be a cheap shared sentinel."""


@dataclass(frozen=True, slots=True)
class Warning:
    """A symptom fired but a grant suppressed it (M5).

    Still logged + posted as a symptom event for audit, but does not flip
    the apoptosis flag. No M2.2 check produces this directly — the M5
    grant-application wrapper demotes Terminals into Warnings when a grant
    applies to the symptom.
    """

    symptom: SymptomType
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Terminal:
    """A symptom fired and no grant applies — apoptosis should engage."""

    symptom: SymptomType
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


CheckResult = Healthy | Warning | Terminal

# Shared sentinel — `is HEALTHY` works because Healthy is frozen + has no
# state, and we never construct another instance from inside this module.
HEALTHY: Final[Healthy] = Healthy()


# --- the checks -----------------------------------------------------------


def check_loop(state: WatcherState, policy: Policy) -> CheckResult:
    """Loop detection: trigger if any signature appears ≥ `max_loop_repeats`
    times in the current ring-buffer window.

    The ring buffer (`state.loop_signatures`) is updated on every tool call
    in `WatcherState.record_tool_call`; its maxlen comes from
    `policy.thresholds.loop_window_actions` (wired in `WatcherState.__post_init__`).

    Counts the *most frequent* signature, not the latest — an agent
    alternating between two looping branches still trips this even though
    no single signature occupies the whole window.
    """
    if not state.loop_signatures:
        return HEALTHY
    threshold = policy.thresholds.max_loop_repeats
    # Counter.most_common(1) is O(n) where n = window size (≤ 40 even on
    # the permissive policy). Plenty fast for the hot path.
    most_common_sig, count = Counter(state.loop_signatures).most_common(1)[0]
    if count >= threshold:
        return Terminal(
            symptom=SymptomType.LOOP,
            reason=(
                f"signature {most_common_sig!r} repeated {count}x in last "
                f"{len(state.loop_signatures)} actions (cap {threshold})"
            ),
            detail={
                "signature": most_common_sig,
                "count": count,
                "window_size": len(state.loop_signatures),
                "max_loop_repeats": threshold,
            },
        )
    return HEALTHY


def check_cost_runaway(state: WatcherState, policy: Policy) -> CheckResult:
    """Token + dollar runaway: trigger on EITHER limit (`max_cost_usd` OR
    `max_tokens_per_run`).

    Reported as `SymptomType.TOKEN_RUNAWAY` for both — the `reason` string
    distinguishes which limit fired so operators can tell from the death cert.

    Cost is the canonical signal (operator pays for cost, not tokens), so
    it's checked first. Token cap is the belt-and-suspenders backstop for
    cases where pricing is wrong/missing (zero-cost models, untracked
    providers — `pricing.cost_for_usage` returns 0 for unknown models).
    """
    t = policy.thresholds
    if state.total_cost_usd >= t.max_cost_usd:
        return Terminal(
            symptom=SymptomType.TOKEN_RUNAWAY,
            reason=(
                f"cumulative cost ${state.total_cost_usd:.4f} ≥ "
                f"cap ${t.max_cost_usd:.2f}"
            ),
            detail={
                "trigger": "cost",
                "cost_usd": state.total_cost_usd,
                "cap_usd": t.max_cost_usd,
                "total_input_tokens": state.total_input_tokens,
                "total_output_tokens": state.total_output_tokens,
            },
        )
    total_tokens = state.total_input_tokens + state.total_output_tokens
    if total_tokens >= t.max_tokens_per_run:
        return Terminal(
            symptom=SymptomType.TOKEN_RUNAWAY,
            reason=(
                f"cumulative tokens {total_tokens:,} ≥ "
                f"cap {t.max_tokens_per_run:,}"
            ),
            detail={
                "trigger": "tokens",
                "total_tokens": total_tokens,
                "total_input_tokens": state.total_input_tokens,
                "total_output_tokens": state.total_output_tokens,
                "cap_tokens": t.max_tokens_per_run,
            },
        )
    return HEALTHY


def check_wall_clock(state: WatcherState, policy: Policy) -> CheckResult:
    """Wall-clock runaway: trigger when uptime exceeds `max_runtime_seconds`.

    Uses `time.monotonic`-based uptime (set in `WatcherState.__init__` via
    `started_monotonic`) so a system clock jump can't suppress or spuriously
    trip the check.
    """
    runtime = state.uptime_seconds()
    cap = policy.thresholds.max_runtime_seconds
    if runtime > cap:
        return Terminal(
            symptom=SymptomType.WALL_CLOCK,
            reason=f"runtime {runtime:.1f}s > cap {cap}s",
            detail={"runtime_seconds": runtime, "cap_seconds": cap},
        )
    return HEALTHY


def check_tool_scope(tool_name: str, policy: Policy) -> CheckResult:
    """Tool scope violation: trigger when `tool_name` is not in the policy's
    allowlist.

    **Empty allowlist means "any tool is allowed"** — the permissive policy
    relies on this. Non-empty = strict membership. This is the only check
    that takes a tool name rather than a state, because it's called at
    `on_tool_start` *before* the tool runs (so we can block it before any
    side effects).
    """
    allowlist = policy.tool_allowlist
    if not allowlist:  # opt-out: empty list disables the check
        return HEALTHY
    if tool_name in allowlist:
        return HEALTHY
    return Terminal(
        symptom=SymptomType.TOOL_SCOPE_VIOLATION,
        reason=f"tool {tool_name!r} not in policy allowlist",
        detail={"tool": tool_name, "allowlist": list(allowlist)},
    )


# --- orchestrator ---------------------------------------------------------


def run_all(state: WatcherState, policy: Policy) -> list[Terminal | Warning]:
    """Run every state-only check; return all non-Healthy verdicts.

    `check_tool_scope` is excluded — it needs the inbound tool name, which
    only the LangChain handler has at `on_tool_start`. The handler calls
    `check_tool_scope` directly and then calls `run_all` for everything else.

    The caller (M2.3 apoptosis wiring) treats the first `Terminal` in the
    returned list as the kill trigger; `Warning`s are logged but don't kill.
    """
    out: list[Terminal | Warning] = []
    for fn in (check_loop, check_cost_runaway, check_wall_clock):
        result = fn(state, policy)
        if not isinstance(result, Healthy):
            out.append(result)
    return out


# --- M5: grant application ----------------------------------------------


def apply_grants(
    results: list[Terminal | Warning],
    grants: list[dict[str, Any]],
) -> list[Terminal | Warning]:
    """Demote Terminal verdicts into Warnings when an active grant covers
    the symptom.

    Pure function — no I/O, no state mutation. Inputs:
      * `results` from `run_all()` or `check_tool_scope()`.
      * `grants` is the SDK's cached `state.grants` list (refreshed by
        each heartbeat response).

    A grant entry shape:
        {"id": "<uuid>", "symptoms": ["loop", ...], "expires_at": "...", "reason": "..."}

    Demotion rule: if ANY grant in the cache lists the result's symptom
    by name, the Terminal becomes a Warning carrying the first matching
    grant_id in `detail["grant_id"]`. The Warning still gets recorded as
    a symptom event for audit — the death cert shows what *would have*
    killed the agent even when a grant let it live.

    Manual kill (M4) does not flow through this function — the kill
    poller calls `request_termination()` directly. So a grant cannot
    suppress an operator-issued kill, which is the security invariant
    in `ApoptosisProofingDefaults`'s docstring.

    **Snapshot semantics:** `grants` is the caller's snapshot — typically
    `state.grants` at the moment `_apply_results` ran. Don't reach back
    to `state.grants` from inside this function; another heartbeat could
    replace it mid-decision and tear the verdict.
    """
    if not grants:
        return results

    # Index grants by symptom for O(1) lookup. We keep the first matching
    # grant per symptom — sufficient for `detail["grant_id"]`.
    by_symptom: dict[str, dict[str, Any]] = {}
    for g in grants:
        for s in g.get("symptoms", []):
            by_symptom.setdefault(str(s), g)

    out: list[Terminal | Warning] = []
    for r in results:
        if isinstance(r, Terminal) and r.symptom.value in by_symptom:
            grant = by_symptom[r.symptom.value]
            out.append(
                Warning(
                    symptom=r.symptom,
                    reason=f"suppressed by grant: {r.reason}",
                    detail={
                        **r.detail,
                        "grant_id": grant.get("id"),
                        "grant_reason": grant.get("reason"),
                    },
                )
            )
        else:
            out.append(r)
    return out
