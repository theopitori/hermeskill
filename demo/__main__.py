"""``python -m demo`` — watch Caspase catch a runaway agent, in one command.

Boots an in-process SQLite control plane (no Postgres), registers an agent,
drives the **real** Caspase engine into a terminal symptom, shows the
cooperative block directive it issues, and files an auditable death
certificate — the whole detect → block → autopsy story in one terminal.

What this proves and what it doesn't: the detection, the block directive, and
the forensic certificate are all real. No separate agent *process* is spawned
or force-killed here — the demo drives the engine directly, and the kill path
shown is the **cooperative** one (the agent is asked to stop). True OS-level
hard-kill (subprocess SIGKILL) is the Phase-2 roadmap item. See the README's
"What the kill actually does" section.

    uv run python -m demo                 # the loop kill (default)
    uv run python -m demo --scenario cost # cost-cap kill
    uv run python -m demo --list          # show available scenarios

No LLM key, no network beyond localhost, fully deterministic — which is what
makes it safe to record as the README GIF and to assert on in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from demo._style import RULE as _RULE
from demo._style import (
    bold as _bold,
)
from demo._style import (
    cyan as _cyan,
)
from demo._style import (
    dim as _dim,
)
from demo._style import (
    green as _green,
)
from demo._style import (
    prepare_console as _prepare_console,
)
from demo._style import (
    red as _red,
)
from demo._style import (
    yellow as _yellow,
)
from demo.calibrate import run_calibrate_demo
from demo.coding_agent._bootstrap import (
    _DEV_DEVELOPER_KEY,
    start_control_plane,
)
from demo.hardkill import run_hardkill_demo
from demo.manualkill import run_manualkill_demo
from demo.rogue import (
    DEFAULT_SCENARIO,
    SCENARIOS,
    ScenarioResult,
    new_state,
    run_scenario,
)

_BASE_URL = "http://localhost:8000"
_AGENT_NAME = "demo-rogue-coder"

_SCENARIO_BLURB = {
    "loop": "strict policy caps identical tool calls at 3 — the agent gets stuck "
    "re-reading the same file and Caspase pulls the plug on the 3rd call.",
    "cost": "strict policy caps spend at $2.00 — the agent burns expensive tokens "
    "until cumulative cost crosses the cap.",
    "scope": "strict policy allows only read_file + search — the agent reaches for "
    "run_bash and is blocked before the tool ever runs.",
    "wall_clock": "strict policy caps wall-clock at 300s — the agent overruns its "
    "time budget and is terminated.",
    "hardkill": "L3 supervisor: a CPU-wedged agent that ignores cooperative "
    "shutdown is force-killed (SIGKILL) in its child process.",
    "manualkill": "operator override: a well-behaved agent is terminated by hand "
    "via `caspase kill` — it stops cooperatively at the next tool call (no symptom).",
    "calibrate": "feedback loop: files several loop-kills, labels most "
    "false-positive via the real feedback endpoint, then shows the advisory "
    "'raise the loop cap' suggestion Caspase derives — suggest-only, never auto-applied.",
}

# Engine scenarios (run in-process via the watcher), the L3 hardkill scenario
# (spawns + supervises a real child process), and the Phase-4 calibrate
# scenario (files labelled kills, then surfaces a tuning suggestion).
_ALL_SCENARIOS = (*SCENARIOS, "hardkill", "manualkill", "calibrate")


@dataclass(slots=True)
class DemoOutcome:
    """End-to-end result — returned for the CI smoke test to assert on."""

    result: ScenarioResult
    kill_event_id: int | None


async def run_offline_demo(
    scenario: str = DEFAULT_SCENARIO,
    *,
    quiet: bool = False,
) -> DemoOutcome:
    """Boot the control plane, run the scenario to a kill, post the death cert.

    Returns a :class:`DemoOutcome`. ``quiet=True`` suppresses the narrative
    (used by the smoke test).
    """
    if not quiet:
        _prepare_console()

    def say(*args: object) -> None:
        if not quiet:
            print(*args)

    # Import the SDK client lazily so a missing control-plane env can't break
    # `--list` / `--help`.
    from caspase.apoptosis import build_death_certificate, build_kill_event_payload
    from caspase.client import CaspaseClient

    say()
    say(_bold(_cyan("  CASPASE")) + _dim("  ·  offline apoptosis demo"))
    say(_dim(f"  policy: strict   scenario: {scenario}"))
    say(_dim("  " + _RULE))
    say()

    say(f"{_cyan('▸')} booting in-process control plane "
        f"{_dim('(sqlite, no postgres)')} …")
    # Force SQLite so the demo honours its "no Postgres" promise even when the
    # surrounding shell/CI exports a CASPASE_DB_URL pointing at Postgres. This
    # matches the path _bootstrap computes, so its stale-db cleanup still works.
    _demo_db = Path(tempfile.gettempdir()) / "caspase-demo.db"
    os.environ["CASPASE_DB_URL"] = f"sqlite+aiosqlite:///{_demo_db}"
    server, serve_task = await start_control_plane()
    say(f"  {_green('✓')} control plane up at {_dim(_BASE_URL)}")

    os.environ["CASPASE_API_KEY"] = _DEV_DEVELOPER_KEY
    os.environ["CASPASE_BASE_URL"] = _BASE_URL

    client = CaspaseClient.from_config()
    kill_event_id: int | None = None
    try:
        say(f"{_cyan('▸')} registering agent {_bold(_AGENT_NAME)} …")
        reg = await client.register_agent(name=_AGENT_NAME, policy_name="strict")
        agent_id = reg.agent_id
        say(f"  {_green('✓')} agent {_dim(str(agent_id))} registered")
        say()
        say(_dim(f"  {_SCENARIO_BLURB.get(scenario, '')}"))
        say()
        say(_bold("  the agent starts working, then misbehaves:"))
        say()

        # --- drive the REAL engine into the kill ---------------------------
        state = new_state("strict", agent_id, _AGENT_NAME)
        result = run_scenario(scenario, state)

        for step in result.steps:
            num = _dim(f"  {step.index:02d}")
            if step.verdict is not None:
                tag = _red(f"☠ {step.verdict.symptom.value.upper()}")
                say(f"{num}  {step.label:<44} {tag}")
            else:
                say(f"{num}  {step.label:<44} {_green('ok')}")

        terminal = result.terminal
        if terminal is None:
            say()
            say(_red("  scenario did not produce a kill — this is a bug"))
            return DemoOutcome(result=result, kill_event_id=None)

        say()
        say(_red("  ⚡ apoptosis: ") + terminal.reason)
        block = {
            "action": "block",
            "message": f"caspase apoptosis: {terminal.reason} End the session.",
        }
        say(_dim("  block directive → ") + _yellow(str(block)))
        say()

        # --- file the death certificate with the control plane -------------
        say(f"{_cyan('▸')} posting death certificate …")
        with contextlib.suppress(Exception):
            await client.post_events(agent_id, state.drain_events())
        payload = build_kill_event_payload(state)
        posted = await client.post_kill_event(agent_id, payload)
        kill_event_id = posted if isinstance(posted, int) else posted.id
        say(f"  {_green('✓')} kill_event {_bold('#' + str(kill_event_id))} filed")

        # --- the autopsy ---------------------------------------------------
        cert = build_death_certificate(state)
        say()
        say(_dim(f"  ┌─ DEATH CERTIFICATE {'─' * 39}"))
        say(_dim("  │ ") + f"{'agent':<10} {agent_id}")
        say(_dim("  │ ") + f"{'trigger':<10} "
            f"{cert.trigger_type.value} / {terminal.symptom.value}")
        say(_dim("  │ ") + f"{'reason':<10} {cert.trigger_reason}")
        say(_dim("  │ ") + f"{'symptoms':<10} {len(cert.symptoms_log)} terminal")
        for s in cert.symptoms_log:
            say(_dim("  │   • ") + f"{s['symptom']}  {_dim(s['reason'])}")
        say(_dim("  │ ") + f"{'shutdown':<10} {len(cert.shutdown_log)} step(s)")
        for st in cert.shutdown_log:
            say(_dim("  │   • ") + str(st.step))
        say(_dim(f"  └{'─' * 58}"))
        say()
        say(_dim("  inspect it:  ") + f"caspase logs {agent_id}")
        say()
        say(_dim("  note: the detection, block directive, and certificate above are"))
        say(_dim("  real. no agent process was force-killed — this is the cooperative"))
        say(_dim("  block path. for an OS-level SIGKILL of a wedged agent that ignores"))
        say(_dim("  cooperative shutdown, run:  ") + "uv run python -m demo --scenario hardkill")
        say()
        return DemoOutcome(result=result, kill_event_id=kill_event_id)
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    parser = argparse.ArgumentParser(
        prog="python -m demo",
        description="Watch Caspase catch a runaway agent and file its death "
        "certificate — offline, one command.",
    )
    parser.add_argument(
        "--scenario",
        choices=_ALL_SCENARIOS,
        default=DEFAULT_SCENARIO,
        help=f"which symptom to trigger (default: {DEFAULT_SCENARIO})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list available scenarios and exit",
    )
    args = parser.parse_args(argv)

    if args.list:
        print("scenarios:")
        for name in _ALL_SCENARIOS:
            print(f"  {name:<12} {_SCENARIO_BLURB.get(name, '')}")
        return 0

    try:
        if args.scenario == "hardkill":
            hk = asyncio.run(run_hardkill_demo())
            return 0 if hk.killed else 1
        if args.scenario == "manualkill":
            mk = asyncio.run(run_manualkill_demo())
            return 0 if mk.killed else 1
        if args.scenario == "calibrate":
            cal = asyncio.run(run_calibrate_demo())
            return 0 if cal.loop_suggested_value is not None else 1
        outcome = asyncio.run(run_offline_demo(args.scenario))
    except KeyboardInterrupt:
        return 130
    return 0 if outcome.result.killed else 1


if __name__ == "__main__":
    sys.exit(main())
