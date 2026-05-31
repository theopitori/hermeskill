"""``python -m demo --scenario manualkill`` — the operator pulls the plug (M4).

The other scenarios show Caspase deciding *on its own* that an agent has gone
rogue (loop, cost, wall-clock, scope). This one shows the **human override**:
an operator runs ``caspase kill <agent_id>`` against a perfectly well-behaved
agent and it dies cooperatively at the next tool boundary.

It exercises the real two-sided M4 path end-to-end, offline:

  operator    POST /agents/{id}/terminate          (== ``caspase kill``)
                       │  creates an INITIATED kill_event
                       ▼
  agent SDK   GET  /kills/pending                   (the KillPendingPoller)
                       │  state.request_termination(manual_kill=…)
                       ▼
  next tool   pre_tool_call → {"action": "block"}   (cooperative L1 gate)
                       │
                       ▼
  agent SDK   POST /agents/{id}/kill_events          confirms the cert
                                                      (status → CONFIRMED)

Nothing is force-killed — manual kill is the *cooperative* path. The agent is
asked to stop and does so at its next checkpoint; the L3 supervisor
(``--scenario hardkill``) is the separate, harder story for agents that refuse.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from demo._style import RULE, bold, cyan, dim, green, prepare_console, red, yellow
from demo.coding_agent._bootstrap import (
    _DEV_DEVELOPER_KEY,
    _DEV_OPERATOR_KEY,
    start_control_plane,
)

_BASE_URL = "http://localhost:8000"
_AGENT_NAME = "demo-operator-kill"
_OPERATOR_REASON = "operator: suspected prompt-injection in the open PR, pull the plug"


@dataclass(slots=True)
class ManualKillOutcome:
    """End-to-end result — returned for the CI smoke test to assert on."""

    killed: bool
    trigger: str
    kill_event_id: int | None
    operator_reason: str | None


async def run_manualkill_demo(*, quiet: bool = False) -> ManualKillOutcome:
    """Register an agent, have an operator kill it, confirm the cooperative death."""
    if not quiet:
        prepare_console()

    def say(*args: object) -> None:
        if not quiet:
            print(*args)

    from caspase.apoptosis import build_death_certificate, build_kill_event_payload
    from caspase.client import CaspaseClient
    from caspase.policies import resolve_policy
    from caspase.watcher import WatcherState

    say()
    say(bold(cyan("  CASPASE")) + dim("  ·  operator manual kill (M4)"))
    say(dim("  policy: strict   scenario: manualkill"))
    say(dim("  " + RULE))
    say()
    say(dim("  Not every kill is automatic. Sometimes a human sees something the"))
    say(dim("  symptom checks can't — and needs an off switch. `caspase kill` is"))
    say(dim("  that switch: the agent keeps working until the operator decides, then"))
    say(dim("  stops cooperatively at its next tool call. No symptom required."))
    say()

    say(f"{cyan('▸')} booting in-process control plane {dim('(sqlite, no postgres)')} …")
    _demo_db = Path(tempfile.gettempdir()) / "caspase-demo.db"
    os.environ["CASPASE_DB_URL"] = f"sqlite+aiosqlite:///{_demo_db}"
    server, serve_task = await start_control_plane()
    os.environ["CASPASE_API_KEY"] = _DEV_DEVELOPER_KEY
    os.environ["CASPASE_BASE_URL"] = _BASE_URL
    say(f"  {green('✓')} control plane up at {dim(_BASE_URL)}")

    client = CaspaseClient.from_config()
    kill_event_id: int | None = None
    try:
        reg = await client.register_agent(name=_AGENT_NAME, policy_name="strict")
        agent_id = reg.agent_id
        say(f"  {green('✓')} agent {dim(str(agent_id))} registered")
        say()

        # The agent is behaving — a normal tool call, no symptom fires.
        state = WatcherState(
            agent_id=agent_id, name=_AGENT_NAME, policy=resolve_policy("strict")
        )
        state.record_tool_call("read_file", {"path": "src/app.py"})
        say(f"{cyan('▸')} agent is working normally "
            f"{dim('(read_file → ok, no symptom)')}")
        say()

        # --- operator side: `caspase kill <agent_id> --reason ...` --------------
        # Manual kill is operator-only — `caspase kill` uses an operator-role
        # key, distinct from the agent's developer key. Mirror that here with a
        # second client so the role boundary is exercised, not bypassed.
        say(f"{cyan('▸')} operator runs {bold('caspase kill ' + str(agent_id))}")
        say(dim(f"  reason: {_OPERATOR_REASON}"))
        async with CaspaseClient(
            base_url=_BASE_URL, api_key=_DEV_OPERATOR_KEY
        ) as operator:
            await operator.terminate_agent(agent_id, reason=_OPERATOR_REASON)
        say(f"  {green('✓')} POST /terminate → kill_event staged "
            f"{dim('(status: INITIATED)')}")
        say()

        # --- agent side: the KillPendingPoller delivers it ----------------------
        # Mirrors caspase.watcher.KillPendingPoller._tick exactly: poll the
        # batch endpoint, then request_termination with the operator context so
        # the cert is stamped MANUAL with the operator's reason.
        say(f"{cyan('▸')} agent SDK polls {bold('GET /kills/pending')} …")
        pending = await client.list_pending_kills()
        entry = next(e for e in pending if str(e.agent_id) == str(agent_id))
        state.request_termination(
            f"manual kill: {entry.operator_reason or entry.trigger_reason}",
            kill_event_id=str(entry.kill_event_id),
            manual_kill={
                "operator": entry.operator,
                "operator_reason": entry.operator_reason,
                "kill_event_id": entry.kill_event_id,
            },
        )
        say(f"  {green('✓')} poller delivered the kill → apoptosis flag set")
        say()

        # --- the cooperative block at the next tool boundary --------------------
        block = {
            "action": "block",
            "message": f"caspase apoptosis: {state.terminate_reason} End the session.",
        }
        say(red("  ⚡ apoptosis: ") + (state.terminate_reason or ""))
        say(dim("  next pre_tool_call → ") + yellow(str(block)))
        say()

        # --- confirm the death certificate -------------------------------------
        say(f"{cyan('▸')} confirming death certificate …")
        with contextlib.suppress(Exception):
            await client.post_events(agent_id, state.drain_events())
        posted = await client.post_kill_event(agent_id, build_kill_event_payload(state))
        kill_event_id = posted if isinstance(posted, int) else posted.id
        say(f"  {green('✓')} kill_event {bold('#' + str(kill_event_id))} confirmed "
            f"{dim('(status: CONFIRMED)')}")

        cert = build_death_certificate(state)
        say()
        say(dim(f"  ┌─ DEATH CERTIFICATE {'─' * 39}"))
        say(dim("  │ ") + f"{'agent':<10} {agent_id}")
        say(dim("  │ ") + f"{'trigger':<10} {cert.trigger_type.value} / manual_kill")
        say(dim("  │ ") + f"{'operator':<10} {cert.operator_reason or '-'}")
        say(dim("  │ ") + f"{'shutdown':<10} {len(cert.shutdown_log)} step(s)")
        for st in cert.shutdown_log:
            say(dim("  │   • ") + str(st.step))
        say(dim(f"  └{'─' * 58}"))
        say()
        say(dim("  inspect it:  ") + f"caspase logs {agent_id}")
        say()
        say(dim("  note: manual kill is the cooperative path — the agent is asked to"))
        say(dim("  stop and does, at its next tool boundary. For an agent that refuses"))
        say(dim("  to cooperate, see:  ") + "uv run python -m demo --scenario hardkill")
        say()

        return ManualKillOutcome(
            killed=state.terminate_requested,
            trigger=cert.trigger_type.value,
            kill_event_id=kill_event_id,
            operator_reason=entry.operator_reason,
        )
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)
