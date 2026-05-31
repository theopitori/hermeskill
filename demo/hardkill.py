"""``python -m demo --scenario hardkill`` — the honest hard kill, demonstrated.

The other scenarios show the *cooperative* path (a block directive the agent is
asked to honour). This one shows the case that path can't handle: an agent
wedged in CPU-bound code that ignores cooperative shutdown entirely. The L3
:class:`~caspase.supervisor.ProcessSupervisor` runs it in a child process and
escalates to an OS-level **SIGKILL** the agent cannot catch — then files a death
certificate whose shutdown log records the real ``supervisor_sigterm`` →
``supervisor_sigkill`` sequence.

The supervised agent target is module-level (spawn re-imports it in the child).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from demo._style import RULE, bold, cyan, dim, green, prepare_console, red
from demo.coding_agent._bootstrap import _DEV_DEVELOPER_KEY, start_control_plane

_BASE_URL = "http://localhost:8000"
_AGENT_NAME = "demo-wedged-agent"


def cpu_wedged_agent(heartbeat: Any) -> None:
    """A genuinely stuck agent: one heartbeat, then a CPU-bound spin forever.

    It ignores SIGTERM (POSIX) so cooperative shutdown provably fails — only
    SIGKILL ends it. This is the exact shape the in-process watchdog's
    ``task.cancel()`` cannot interrupt: no awaits, no cooperation. On Windows
    there is no catchable SIGTERM, so ``terminate()`` is already a hard kill.
    """
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    heartbeat.beat()  # proves it was alive, then never beats again
    while True:
        pass


@dataclass(slots=True)
class HardkillOutcome:
    killed: bool
    sigkilled: bool
    trigger: str
    kill_event_id: int | None


async def run_hardkill_demo(*, quiet: bool = False) -> HardkillOutcome:
    """Spawn a wedged agent, hard-kill it with the supervisor, file the cert."""
    if not quiet:
        prepare_console()

    def say(*args: object) -> None:
        if not quiet:
            print(*args)

    from caspase.apoptosis import build_death_certificate, build_kill_event_payload
    from caspase.client import CaspaseClient
    from caspase.policies import resolve_policy
    from caspase.supervisor import ProcessSupervisor
    from caspase.types import SymptomType
    from caspase.watcher import WatcherState

    say()
    say(bold(cyan("  CASPASE")) + dim("  ·  hard-kill supervisor (L3)"))
    say(dim("  policy: strict   scenario: hardkill"))
    say(dim("  " + RULE))
    say()
    say(dim("  Some agents can't be asked nicely. A loop of CPU-bound work with no"))
    say(dim("  awaits ignores the cooperative block AND the L2 task.cancel() — there"))
    say(dim("  is no await point to raise at. The only honest answer is to kill the"))
    say(dim("  OS process from the outside. That's what L3 does."))
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
        state = WatcherState(
            agent_id=agent_id, name=_AGENT_NAME, policy=resolve_policy("strict")
        )

        reason = "agent wedged in CPU-bound code — no heartbeat for 0.4s"
        state.request_termination(f"heartbeat_stale: {reason}")
        state.record_symptom(
            symptom=SymptomType.HEARTBEAT_STALE,
            severity="terminal",
            reason=reason,
            detail={"heartbeat_timeout_seconds": 0.4},
        )

        supervisor = ProcessSupervisor(
            grace_seconds=0.5,
            heartbeat_timeout_seconds=0.4,
            wall_clock_seconds=5.0,  # backstop: the demo can never hang
            record_step=lambda step, detail: state.record_shutdown_step(step, **detail),
        )

        # The supervisor uses the 'spawn' start method: the child is a fresh
        # interpreter that re-imports this module (``demo.hardkill``). Make the
        # repo root importable in that child regardless of how the parent was
        # launched — ``python -m demo`` puts cwd on sys.path, but the ``pytest``
        # console-script (how CI runs the smoke test) does not, so the child
        # would otherwise die with ``ModuleNotFoundError: No module named
        # 'demo'`` and report ``trigger='completed'`` instead of a real kill.
        #
        # Two complementary guarantees, because ``multiprocessing.spawn`` both
        # ships the parent's in-memory ``sys.path`` to the child *and* lets the
        # child honour ``PYTHONPATH`` at interpreter startup:
        #   1. insert into this process's ``sys.path`` so the captured copy the
        #      child restores already contains the repo root, and
        #   2. export ``PYTHONPATH`` so a fresh child resolves it even before
        #      that restore runs.
        _repo_root = str(Path(__file__).resolve().parents[1])
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)
        _existing_pp = os.environ.get("PYTHONPATH", "")
        if _repo_root not in _existing_pp.split(os.pathsep):
            os.environ["PYTHONPATH"] = (
                _repo_root + (os.pathsep + _existing_pp if _existing_pp else "")
            )

        say(f"{cyan('▸')} spawning the wedged agent in a child process …")
        # Supervise off the event loop so the control plane stays responsive.
        result = await asyncio.to_thread(
            supervisor.run, cpu_wedged_agent, (), use_heartbeat=True
        )

        say(f"  {dim('child pid')} … wedged: one heartbeat, then a CPU spin")
        say(f"  {red('✗')} cooperative shutdown: ignored "
            f"{dim('(SIGTERM swallowed; no await to cancel)')}")
        verdict = "SIGKILL" if result.sigkilled else "terminate()"
        say(f"  {green('✓')} L3 supervisor: process killed via {bold(verdict)} "
            f"after {result.duration_seconds:.2f}s "
            f"{dim('(trigger: ' + result.trigger + ')')}")
        say()

        say(f"{cyan('▸')} posting death certificate …")
        with contextlib.suppress(Exception):
            await client.post_events(agent_id, state.drain_events())
        posted = await client.post_kill_event(agent_id, build_kill_event_payload(state))
        kill_event_id = posted if isinstance(posted, int) else posted.id
        say(f"  {green('✓')} kill_event {bold('#' + str(kill_event_id))} filed")

        cert = build_death_certificate(state)
        say()
        say(dim(f"  ┌─ DEATH CERTIFICATE {'─' * 39}"))
        say(dim("  │ ") + f"{'agent':<10} {agent_id}")
        say(dim("  │ ") + f"{'trigger':<10} {cert.trigger_type.value} / heartbeat_stale")
        say(dim("  │ ") + f"{'shutdown':<10} {len(cert.shutdown_log)} step(s)")
        for st in cert.shutdown_log:
            say(dim("  │   • ") + str(st.step))
        say(dim(f"  └{'─' * 58}"))
        say()
        say(dim("  note: this time a real OS process was force-killed — the kill L1/L2"))
        say(dim("  cannot perform. Cooperative shutdown stays the default; the process"))
        say(dim("  supervisor is opt-in for untrusted or wedge-prone agents."))
        say()

        return HardkillOutcome(
            killed=result.killed,
            sigkilled=result.sigkilled,
            trigger=result.trigger,
            kill_event_id=kill_event_id,
        )
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)
