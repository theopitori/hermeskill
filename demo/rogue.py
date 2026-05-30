"""Scripted, offline rogue agent — the engine behind ``python -m demo``.

This module drives the **real** Caspase detection engine (``caspase.checks``
over a real ``WatcherState``) into each terminal symptom, deterministically and
with no LLM and no network beyond the in-process control plane. It is the
runtime-agnostic core: no Hermes, no LangGraph — just the SDK doing the kill,
exactly as any framework adapter would trigger it.

The orchestration (booting the control plane, printing the narrative) lives in
``demo.__main__``. Keeping the scenario logic here means the CI smoke test can
import :func:`run_scenario` and assert on the verdict without scraping stdout.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from caspase.checks import Terminal, Warning, check_tool_scope, run_all
from caspase.policies import resolve_policy
from caspase.watcher import WatcherState

# Scenarios the demo can run. ``loop`` is the headline (and the GIF subject);
# the others exist so every shipped symptom has a one-command repro.
SCENARIOS = ("loop", "cost", "scope", "wall_clock")
DEFAULT_SCENARIO = "loop"

# A model present in caspase.pricing so cost actually accrues in the cost demo.
_EXPENSIVE_MODEL = "claude-opus-4-7"  # $15 / $75 per 1M tok


@dataclass(frozen=True, slots=True)
class Step:
    """One observable action the rogue agent took, plus the verdict it drew.

    ``__main__`` renders these into the terminal narrative; the smoke test
    inspects ``verdict``.
    """

    index: int
    label: str
    verdict: Terminal | Warning | None = None


@dataclass(slots=True)
class ScenarioResult:
    """Outcome of running a scenario to its kill."""

    scenario: str
    state: WatcherState
    steps: list[Step] = field(default_factory=list)
    terminal: Terminal | None = None

    @property
    def killed(self) -> bool:
        return self.terminal is not None


def new_state(policy_name: str, agent_id: UUID, name: str) -> WatcherState:
    """Construct a real WatcherState the checks run against."""
    return WatcherState(
        agent_id=agent_id,
        name=name,
        policy=resolve_policy(policy_name),
    )


def _commit_terminal(state: WatcherState, verdict: Terminal) -> None:
    """Record the symptom + flip the apoptosis flag, mirroring what a
    framework adapter (e.g. ``caspase_hermes.bridge``) does on a Terminal."""
    state.record_symptom(
        symptom=verdict.symptom,
        severity="terminal",
        reason=verdict.reason,
        detail=verdict.detail,
    )
    if not state.terminate_requested:
        state.request_termination(verdict.reason)


def _run_loop(state: WatcherState) -> ScenarioResult:
    """Identical-args tool calls until ``check_loop`` trips."""
    result = ScenarioResult(scenario="loop", state=state)
    args = {"path": "README.md"}
    for i in range(1, state.policy.thresholds.max_loop_repeats + 3):
        state.record_tool_call("read_file", args)
        verdict = _first_terminal(run_all(state, state.policy))
        label = "read_file(path='README.md')"
        result.steps.append(Step(index=i, label=label, verdict=verdict))
        if verdict is not None:
            _commit_terminal(state, verdict)
            result.terminal = verdict
            break
    return result


def _run_cost(state: WatcherState) -> ScenarioResult:
    """Expensive LLM calls until cumulative cost crosses the policy cap."""
    result = ScenarioResult(scenario="cost", state=state)
    for i in range(1, 12):
        state.record_llm_call(_EXPENSIVE_MODEL, input_tokens=10_000, output_tokens=20_000)
        verdict = _first_terminal(run_all(state, state.policy))
        label = f"llm_call({_EXPENSIVE_MODEL})  cumulative ${state.total_cost_usd:.2f}"
        result.steps.append(Step(index=i, label=label, verdict=verdict))
        if verdict is not None:
            _commit_terminal(state, verdict)
            result.terminal = verdict
            break
    return result


def _run_scope(state: WatcherState) -> ScenarioResult:
    """A single call to a tool outside the strict allowlist."""
    result = ScenarioResult(scenario="scope", state=state)
    tool = "run_bash"
    verdict = check_tool_scope(tool, state.policy)
    term = verdict if isinstance(verdict, Terminal) else None
    result.steps.append(Step(index=1, label=f"{tool}(cmd='rm -rf /')", verdict=term))
    if term is not None:
        _commit_terminal(state, term)
        result.terminal = term
    return result


def _run_wall_clock(state: WatcherState) -> ScenarioResult:
    """Back-date the start so wall-clock uptime exceeds the cap immediately.

    The check reads ``state.uptime_seconds()`` off a ``time.monotonic`` base;
    rewinding ``started_monotonic`` is the deterministic, no-sleep way to
    simulate an agent that has run past its wall-clock ceiling.
    """
    result = ScenarioResult(scenario="wall_clock", state=state)
    cap = state.policy.thresholds.max_runtime_seconds
    state.started_monotonic -= cap + 1  # now "ran" cap+1 seconds
    state.record_tool_call("search", {"q": "still working..."})
    verdict = _first_terminal(run_all(state, state.policy))
    label = f"search(...)  uptime {state.uptime_seconds():.0f}s > cap {cap}s"
    result.steps.append(Step(index=1, label=label, verdict=verdict))
    if verdict is not None:
        _commit_terminal(state, verdict)
        result.terminal = verdict
    return result


_RUNNERS: dict[str, Callable[[WatcherState], ScenarioResult]] = {
    "loop": _run_loop,
    "cost": _run_cost,
    "scope": _run_scope,
    "wall_clock": _run_wall_clock,
}


def run_scenario(scenario: str, state: WatcherState) -> ScenarioResult:
    """Drive ``state`` through ``scenario`` until the kill fires.

    Pure over the engine — no I/O, no control-plane calls. Returns the steps
    taken and the terminal verdict (``ScenarioResult.killed`` is True on
    success). The caller owns posting the death certificate.
    """
    try:
        runner = _RUNNERS[scenario]
    except KeyError:
        raise ValueError(
            f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}"
        ) from None
    return runner(state)


def _first_terminal(verdicts: list[Terminal | Warning]) -> Terminal | None:
    for v in verdicts:
        if isinstance(v, Terminal):
            return v
    return None
