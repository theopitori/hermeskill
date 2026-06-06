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
framework adapter calls it directly at each tool boundary (M2.3).

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

from hermeskill.types import Policy, SymptomType
from hermeskill.watcher import WatcherState

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


@dataclass(frozen=True, slots=True)
class Steer:
    """A *soft* intervention: a symptom is building toward terminal but hasn't
    crossed the kill line yet, so instead of apoptosis we block the offending
    call and inject a corrective "steer" message — a chance for the agent to
    change course before it gets killed.

    Only `check_loop` produces this today (loops are the one symptom an agent
    can recover from on its own — re-reading the task and trying a different
    tool breaks the cycle). Resource-burn symptoms (cost, wall-clock) and scope
    violations are not steerable: you can't talk an agent out of money already
    spent or a tool it must not touch.

    Like `Terminal`, it's delivered through the framework adapter's block
    primitive — but it does **not** flip `terminate_requested`; the session
    continues.
    """

    symptom: SymptomType
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


CheckResult = Healthy | Warning | Terminal | Steer

# Shared sentinel — `is HEALTHY` works because Healthy is frozen + has no
# state, and we never construct another instance from inside this module.
HEALTHY: Final[Healthy] = Healthy()


# --- the checks -----------------------------------------------------------


def loop_peak(state: WatcherState) -> tuple[str | None, int]:
    """Most-repeated signature in the loop ring buffer and its count.

    The single source of truth for "how close is this agent to the loop
    cap?" — `check_loop` uses it to decide the kill, and `vitals` uses it
    to render the loop-pressure gauge, so the gauge can never disagree with
    the trigger. Returns ``(None, 0)`` for an empty buffer.
    """
    if not state.loop_signatures:
        return None, 0
    # Counter.most_common(1) is O(n) where n = window size (≤ 40 even on
    # the permissive policy). Plenty fast for the hot path.
    most_common_sig, count = Counter(state.loop_signatures).most_common(1)[0]
    return most_common_sig, count


def check_loop(state: WatcherState, policy: Policy) -> CheckResult:
    """Loop detection with a graduated response: **steer** before you kill.

    Two thresholds, two intents:

      * **Terminal (kill)** keys off the *most-frequent* signature in the
        window (`loop_peak`) reaching `max_loop_repeats`. Peak-based so an
        agent alternating between two looping branches still dies — no single
        signature owns the window, but the worst offender crosses the line.

      * **Steer (nudge)** keys off the *current call's* signature reaching
        `loop_steer_repeats` (and below the kill line). Current-signature —
        **not** peak — is essential: a steer blocks the call and is meant to
        be recoverable, so the moment the agent obeys and switches to a
        different call, that new call's count is 1 and it sails through. If
        steer keyed off the peak, the just-evicted-over-a-full-window losing
        signature would keep tripping and we'd block the agent's *corrective*
        call too — defeating the whole point.

    Terminal takes precedence (checked first). `loop_steer_repeats=None`
    disables steering entirely, restoring the original kill-only behaviour.

    The ring buffer (`state.loop_signatures`) is updated on every tool call
    in `WatcherState.record_tool_call`; its maxlen comes from
    `policy.thresholds.loop_window_actions` (wired in `WatcherState.__post_init__`).
    """
    if not state.loop_signatures:
        return HEALTHY
    t = policy.thresholds
    max_repeats = t.max_loop_repeats
    window = len(state.loop_signatures)
    # One pass over the window powers both verdicts (window ≤ 40 even on the
    # permissive policy — cheap on the hot path).
    counts = Counter(state.loop_signatures)
    most_common_sig, peak = counts.most_common(1)[0]
    if peak >= max_repeats:
        return Terminal(
            symptom=SymptomType.LOOP,
            reason=(
                f"signature {most_common_sig!r} repeated {peak}x in last "
                f"{window} actions (cap {max_repeats})"
            ),
            detail={
                "signature": most_common_sig,
                "count": peak,
                "window_size": window,
                "max_loop_repeats": max_repeats,
            },
        )

    steer = t.loop_steer_repeats
    if steer is None:
        return HEALTHY
    current_sig = state.loop_signatures[-1]
    current_count = counts[current_sig]
    # current_count <= peak < max_repeats, so the upper bound holds for free.
    if current_count >= steer:
        remaining = max_repeats - current_count
        return Steer(
            symptom=SymptomType.LOOP,
            reason=(
                f"signature {current_sig!r} repeated {current_count}x in last "
                f"{window} actions (steer at {steer}, kill at {max_repeats}; "
                f"{remaining} more identical repeat(s) → termination)"
            ),
            detail={
                "signature": current_sig,
                "count": current_count,
                "window_size": window,
                "loop_steer_repeats": steer,
                "max_loop_repeats": max_repeats,
                "remaining_before_kill": remaining,
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


def run_all(state: WatcherState, policy: Policy) -> list[Terminal | Warning | Steer]:
    """Run every state-only check; return all non-Healthy verdicts.

    `check_tool_scope` is excluded — it needs the inbound tool name, which
    only the framework adapter has at the tool boundary. The adapter calls
    `check_tool_scope` directly and then calls `run_all` for everything else.

    The caller (M2.3 apoptosis wiring) treats the first `Terminal` in the
    returned list as the kill trigger; a `Steer` is delivered as a corrective
    block but does not kill; `Warning`s are logged but don't act.
    """
    out: list[Terminal | Warning | Steer] = []
    for fn in (check_loop, check_cost_runaway, check_wall_clock):
        result = fn(state, policy)
        if not isinstance(result, Healthy):
            out.append(result)
    return out


# --- M5: grant application ----------------------------------------------


def apply_grants(
    results: list[Terminal | Warning | Steer],
    grants: list[dict[str, Any]],
) -> list[Terminal | Warning | Steer]:
    """Demote Terminal *and* Steer verdicts into Warnings when an active grant
    covers the symptom.

    A loop grant means "this repetition is intentional" (e.g. a polling
    agent). It must suppress not just the kill but the *steer* too — otherwise
    a granted, legitimately-looping agent would have its repeated calls blocked
    and be nagged with corrective messages on every tick. So both the hard kill
    and the soft nudge are demoted to an audit-only Warning when granted.

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

    out: list[Terminal | Warning | Steer] = []
    for r in results:
        if isinstance(r, (Terminal, Steer)) and r.symptom.value in by_symptom:
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
