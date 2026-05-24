"""Run the 9-step DoD demo end-to-end.

Steps 1-4 land in M2.6 (this script today):

    1. Integration — run the demo agent, see registration succeed
    2. Logs — read events from the control plane
    3. Loop induction — `--induce loop` makes the agent self-terminate
    4. Death certificate — read the cert from the control plane

Steps 5-9 are filled in by later milestones:

    5. Webhook delivery       — deferred from MVP (post-v1)
    6. One-click feedback     — M3 (landed; not yet wired into this script)
    7. Manual kill            — M4 (this script)
    8. Grant (apoptosis-proof) — M5
    9. Manual kill bypasses grant — M5

The script is cross-platform (works on Windows + macOS + Linux) — it
shells out to `uv run` to invoke the demo agent + CLI. Prereqs:

    * A `.env` file at the repo root with at minimum:
        STASIS_API_KEY=sk_dev_developer_local_only_do_not_ship
        STASIS_BASE_URL=http://localhost:8000

    * Postgres reachable at `STASIS_DB_URL`, schema upgraded to head:
        uv run --package stasis-control-plane \\
            alembic -c packages/stasis-control-plane/alembic.ini upgrade head

    * The control plane running locally:
        uv run --package stasis-control-plane stasis-control-plane
      (or `python -m control_plane.main`)

Usage:

    uv run python demo/run_dod.py
    uv run python demo/run_dod.py --skip-step 2,3   # skip steps by number

Each step prints a header, runs its command, and either succeeds or
exits non-zero with a diagnostic. The script is intentionally chatty so
the demo is also a piece of documentation.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_OPERATOR_KEY = "sk_dev_operator_local_only_do_not_ship"

# Make stasis_agent + control_plane importable for the API-side queries
# we do in step 4 (so we don't have to shell out for everything).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# --- pretty printing -----------------------------------------------------


def _header(n: int, title: str) -> None:
    line = f"=== STEP {n}: {title} "
    print()
    print(line + "=" * max(0, 70 - len(line)))


def _ok(msg: str) -> None:
    print(f"  ok  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)
    sys.exit(1)


def _run(
    cmd: list[str],
    *,
    expect_returncode: int | None = 0,
    capture: bool = True,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess; fail the demo if returncode doesn't match `expect`.

    `expect_returncode=None` accepts any code (useful for `--induce loop`
    which is supposed to exit 3 on StasisTerminated).
    """
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=False,
    )
    if capture:
        if proc.stdout:
            for line in proc.stdout.splitlines():
                print(f"    | {line}")
        if proc.stderr:
            for line in proc.stderr.splitlines():
                print(f"    ! {line}", file=sys.stderr)
    if expect_returncode is not None and proc.returncode != expect_returncode:
        _fail(
            f"expected exit code {expect_returncode}, got {proc.returncode} "
            f"from: {' '.join(cmd)}"
        )
    return proc


# --- the steps -----------------------------------------------------------


def _load_env() -> None:
    """Read .env so we can read STASIS_BASE_URL / STASIS_API_KEY here too."""
    path = Path(".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def step_1_integration() -> str:
    """DoD #1: the 5-line `watch()` integration registers an agent."""
    _header(1, "integration — `watch()` registers the agent")
    # Healthy run; not yet induced.
    proc = _run(
        ["uv", "run", "python", "demo/coding_agent/agent.py"],
        timeout=120.0,
    )
    # Pull the agent_id printed by `tip: uv run stasis logs <id>`.
    agent_id = _scrape_agent_id(proc.stdout)
    if not agent_id:
        _fail("could not find agent_id in demo output")
    _ok(f"registered agent_id={agent_id}")
    return agent_id


def step_2_logs(agent_id: str) -> None:
    """DoD #2: `stasis logs <id>` shows real-time events."""
    _header(2, "logs — `stasis logs` shows the events")
    proc = _run(
        ["uv", "run", "stasis", "logs", agent_id],
        timeout=30.0,
    )
    # Sanity: the output should include at least one heartbeat or tool_call.
    out = proc.stdout
    if "tool_call" not in out and "heartbeat" not in out and "lifecycle" not in out:
        _fail("no expected event types found in `stasis logs` output")
    _ok("control plane returned events for the agent")


def step_3_loop_induction() -> str:
    """DoD #3: `--induce loop` makes the agent self-terminate cooperatively."""
    _header(3, "loop induction — agent self-terminates")
    proc = _run(
        ["uv", "run", "python", "demo/coding_agent/agent.py", "--induce", "loop"],
        expect_returncode=3,  # demo exits 3 on StasisTerminated
        timeout=120.0,
    )
    # The agent_id is printed in the death-tip line on stderr.
    agent_id = _scrape_agent_id(proc.stdout + proc.stderr)
    if not agent_id:
        _fail("could not find agent_id in death output")
    _ok(f"agent died cooperatively, agent_id={agent_id}")
    return agent_id


def step_4_death_certificate(agent_id: str) -> None:
    """DoD #4: query the death cert from the control plane."""
    _header(4, "death certificate — cert lands on the control plane")
    # Use the SDK client directly rather than CLI (the CLI for death cert
    # arrives in M6; for now the API is the source of truth).
    import asyncio

    from stasis_agent.client import StasisClient

    async def _check() -> None:
        client = StasisClient.from_config()
        try:
            kill_events = await client.list_kill_events(agent_id)
            if not kill_events:
                _fail("no kill_event found for terminated agent")
            ke = kill_events[0]
            if ke.status.value != "confirmed":
                _fail(f"expected confirmed status, got {ke.status.value}")
            if ke.death_certificate is None:
                _fail("kill_event has no death_certificate")
            cert = ke.death_certificate
            symptoms = [s["symptom"] for s in cert.symptoms_log]
            if "loop" not in symptoms:
                _fail(f"loop symptom missing from symptoms_log: {symptoms}")
            _ok(f"kill_event id={ke.id} status={ke.status.value}")
            _ok(f"  trigger_reason: {ke.trigger_reason}")
            _ok(f"  symptoms_log:   {symptoms}")
            shutdown_steps = [s.step for s in cert.shutdown_log]
            _ok(f"  shutdown_log:   {shutdown_steps}")
            # Verify the agent flipped to terminated.
            agent = await client.get_agent(agent_id)
            if agent.status.value != "terminated":
                _fail(f"agent.status={agent.status.value} (expected terminated)")
            _ok("agent.status = terminated")
        finally:
            await client.aclose()

    asyncio.run(_check())


def _launch_and_find_idle_agent() -> tuple[subprocess.Popen[str], str]:
    """Spawn the idle demo agent and return (process, agent_id) once it
    registers in the fleet. Caller is responsible for cleanup.

    Used by manual-kill (step 7) and the M5 demo steps (8 + 9), which all
    need a long-lived agent they can poke from outside.

    **Why we capture launch_time:** a previous step's idle agent that
    was SIGKILL'd (not cooperatively terminated) leaves a stale "running"
    row in the DB. Filtering by `registered_at >= launch_time` makes
    sure we attach to *this* subprocess, not a ghost from a previous one.
    """
    import asyncio
    from datetime import UTC, datetime

    from stasis_agent.client import StasisClient

    # Anchor *before* spawning so a fast-registering agent can't slip
    # under the cutoff. `replace(microsecond=0)` truncates down → moves
    # `launch_time` up to 999ms earlier than wall-clock, which is the
    # buffer we want against test-host vs. control-plane clock skew.
    # Don't "clean up" this truncation — the early bias is the point.
    launch_time = datetime.now(UTC).replace(microsecond=0)

    proc = subprocess.Popen(
        ["uv", "run", "python", "demo/coding_agent/agent.py", "--idle"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    async def _find() -> str:
        async with StasisClient.from_config() as client:
            for _ in range(60):
                fleet = await client.list_agents()
                # Filter by registered_at >= launch_time so a stale row
                # from a previous step doesn't get matched. The fleet is
                # newest-first; the first match is our agent.
                matches = [
                    a
                    for a in fleet
                    if a.name == "demo-coding-bot-idle"
                    and a.registered_at >= launch_time
                ]
                if matches:
                    return str(matches[0].id)
                await asyncio.sleep(0.5)
            _fail("idle agent did not register within 30s")
            return ""  # unreachable

    try:
        agent_id = asyncio.run(_find())
    except Exception:
        # Make sure we don't leak the subprocess if registration polling
        # blew up.
        proc.kill()
        proc.wait(timeout=5.0)
        raise
    return proc, agent_id


def step_7_manual_kill() -> None:
    """DoD #7: operator issues `stasis kill`; the agent dies cooperatively
    and the cert records the operator + reason.

    Flow:
      1. Launch the idle demo agent in the background.
      2. Poll the fleet until it registers; capture its id.
      3. Run `stasis kill <id> --reason "..."` with the operator key.
      4. Wait for the agent process to exit (cooperative — exit code 3).
      5. Read the kill_event and verify trigger_type=manual + operator_reason.
    """
    _header(7, "manual kill — operator-issued cooperative termination")

    import asyncio
    import re as _re

    from stasis_agent.client import StasisClient

    # Step 1: launch idle agent in the background. We bypass _run because
    # it waits for completion; here we need the process alive.
    print("  launching idle agent…")
    agent_proc = subprocess.Popen(
        ["uv", "run", "python", "demo/coding_agent/agent.py", "--idle"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    agent_id: str | None = None
    try:
        # Step 2: wait for the agent to register. The agent prints
        # `tip: uv run stasis logs <id>` on exit, but for kill-mid-run we
        # need to find it earlier — poll the fleet for a `demo-coding-bot-idle`
        # entry. Cap at 30s.
        async def _find_agent() -> str:
            async with StasisClient.from_config() as client:
                for _ in range(60):
                    fleet = await client.list_agents()
                    matches = [
                        a
                        for a in fleet
                        if a.name == "demo-coding-bot-idle"
                        and a.status.value != "terminated"
                    ]
                    if matches:
                        # The newest is at the front (registered_at desc).
                        return str(matches[0].id)
                    await asyncio.sleep(0.5)
                _fail("idle agent did not register within 30s")
                return ""  # unreachable; satisfies type checker

        agent_id = asyncio.run(_find_agent())
        _ok(f"idle agent registered, agent_id={agent_id}")

        # Step 3: issue the kill. Use the operator key — developer key
        # would 403 here. Force UTF-8 on the child stdio so Rich's
        # `✓`/`…` glyphs don't crash on Windows' cp1252 default.
        kill_env = {
            **os.environ,
            "STASIS_API_KEY": _OPERATOR_KEY,
            "PYTHONIOENCODING": "utf-8",
        }
        print("  issuing `stasis kill`…")
        kill_proc = subprocess.run(
            [
                "uv",
                "run",
                "stasis",
                "kill",
                agent_id,
                "--reason",
                "DoD step 7: manual kill demo",
                "--poll-interval",
                "0.5",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=kill_env,
            timeout=120.0,
            check=False,
        )
        if kill_proc.returncode != 0:
            print(kill_proc.stdout)
            print(kill_proc.stderr, file=sys.stderr)
            _fail(f"`stasis kill` exited {kill_proc.returncode}")
        if "confirmed dead" not in kill_proc.stdout:
            _fail("`stasis kill` did not confirm death")
        _ok("CLI reported confirmed dead")

        # Step 4: the agent process should exit on its own with code 3
        # once StasisTerminated bubbles up.
        try:
            agent_proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            agent_proc.kill()
            _fail("idle agent did not exit within 30s after kill")
        if agent_proc.returncode != 3:
            _fail(
                f"agent exited {agent_proc.returncode}, expected 3 "
                f"(StasisTerminated)"
            )
        _ok("agent process exited 3 (StasisTerminated)")

        # Step 5: verify the kill_event carries operator context.
        async def _verify_cert() -> None:
            async with StasisClient.from_config() as client:
                kills = await client.list_kill_events(agent_id)
                if not kills:
                    _fail("no kill_events found")
                ke = kills[0]
                if ke.trigger_type.value != "manual":
                    _fail(f"trigger_type={ke.trigger_type.value} (expected manual)")
                if ke.status.value != "confirmed":
                    _fail(f"status={ke.status.value} (expected confirmed)")
                if not ke.operator_reason:
                    _fail("operator_reason missing on kill_event")
                cert = ke.death_certificate
                if cert is None:
                    _fail("kill_event has no death_certificate")
                assert cert is not None  # narrows for static checkers; _fail exits
                # The SDK should have set both fields based on
                # state.manual_kill from the poller payload.
                if cert.trigger_type.value != "manual":
                    _fail(f"cert.trigger_type={cert.trigger_type.value}")
                _ok(f"kill_event id={ke.id} trigger=manual status=confirmed")
                _ok(f"  operator_reason: {ke.operator_reason}")
                _ok(f"  cert.operator:   {cert.operator}")

        # _re used below if we ever need to parse the CLI output; silence
        # the import-but-unused lint.
        _ = _re
        asyncio.run(_verify_cert())

    finally:
        if agent_proc.poll() is None:
            agent_proc.kill()
            agent_proc.wait(timeout=5.0)


def step_8_grant() -> None:
    """DoD #8: operator issues an apoptosis-proofing grant; the SDK sees
    it via the next heartbeat.

    What this step demonstrates (operator workflow):
      1. `stasis grant <id> --symptoms ... --duration ...` exits 0.
      2. The grant is active in `/agents/{id}/grants?active_only=true`.
      3. The grant lands in the agent's heartbeat response (this is how
         the SDK actually picks it up — see HeartbeatOut.active_grants).

    The "live agent under load actually survives a covered symptom"
    claim is carried by the SDK unit tests (`test_grants.py`) — the
    real-suppression path needs the grant to land *before* the symptom
    fires, which is awkward in a single-shot demo with a 30s default
    heartbeat. The operator-flow + plumbing is what this DoD step
    verifies.

    **Side effect:** this step kills its idle agent subprocess via
    SIGKILL (not cooperative shutdown), so the agent's DB row is left
    in `running` state — no cert ever posts. Step 9's
    `_launch_and_find_idle_agent` uses a `registered_at` filter to
    ignore this ghost row, but `stasis fleet` will show it stale until
    something else terminates it.
    """
    _header(8, "grant — operator issues apoptosis-proofing")

    import asyncio

    from stasis_agent.client import StasisClient

    print("  launching idle agent…")
    agent_proc, agent_id = _launch_and_find_idle_agent()
    _ok(f"idle agent registered, agent_id={agent_id}")

    try:
        # Step 1: issue the grant via the CLI.
        grant_env = {
            **os.environ,
            "STASIS_API_KEY": _OPERATOR_KEY,
            "PYTHONIOENCODING": "utf-8",
        }
        print("  issuing `stasis grant`…")
        grant_proc = subprocess.run(
            [
                "uv",
                "run",
                "stasis",
                "grant",
                agent_id,
                "--symptoms",
                "tool_scope_violation",
                "--duration",
                "1h",
                "--reason",
                "DoD step 8: operator exploring a new tool",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=grant_env,
            timeout=30.0,
            check=False,
        )
        if grant_proc.returncode != 0:
            print(grant_proc.stdout)
            print(grant_proc.stderr, file=sys.stderr)
            _fail(f"`stasis grant` exited {grant_proc.returncode}")
        if "grant issued" not in grant_proc.stdout:
            _fail("`stasis grant` did not confirm issuance")
        _ok("CLI reported grant issued")

        # Step 2: verify the grant is active via the API.
        async def _verify() -> None:
            async with StasisClient.from_config() as client:
                actives = await client.list_grants(agent_id, active_only=True)
                if not actives:
                    _fail("no active grants found for agent")
                g = actives[0]
                if "tool_scope_violation" not in [s.value for s in g.symptoms]:
                    _fail(
                        f"grant symptoms={g.symptoms} missing tool_scope_violation"
                    )
                if not g.active:
                    _fail("grant is not marked active")
                _ok(f"grant id={g.id} active={g.active}")

                # Step 3: heartbeat the agent ourselves and verify the
                # response carries the grant. (The SDK's worker would do
                # this too, but on its 30s cadence — we test the wire
                # directly here.)
                hb = await client._request(
                    "POST",
                    f"/agents/{agent_id}/heartbeat",
                    json={"uptime_seconds": 1.0},
                )
                grants = hb.get("active_grants", [])
                if not grants:
                    _fail("heartbeat response did not include the grant")
                _ok(f"heartbeat carries {len(grants)} active grant(s)")

        asyncio.run(_verify())
    finally:
        if agent_proc.poll() is None:
            agent_proc.kill()
            agent_proc.wait(timeout=5.0)


def step_9_manual_kill_bypasses_grant() -> None:
    """DoD #9: an active grant covers some symptoms, but manual kill
    bypasses every grant. This is the security invariant in
    `ApoptosisProofingDefaults` — operators can always override.

    Flow:
      1. Launch idle agent.
      2. Issue grant covering `tool_scope_violation`.
      3. Issue `stasis kill` — the manual-kill path does NOT route
         through `apply_grants` (it calls `request_termination` directly
         in the SDK poller), so the kill lands.
      4. Verify the agent died with trigger=manual, despite the active
         grant still being in the DB.
    """
    _header(9, "manual kill bypasses grant")

    import asyncio

    from stasis_agent.client import StasisClient

    print("  launching idle agent…")
    agent_proc, agent_id = _launch_and_find_idle_agent()
    _ok(f"idle agent registered, agent_id={agent_id}")

    try:
        op_env = {
            **os.environ,
            "STASIS_API_KEY": _OPERATOR_KEY,
            "PYTHONIOENCODING": "utf-8",
        }

        # Step 1: issue the grant.
        print("  issuing grant…")
        grant_proc = subprocess.run(
            [
                "uv",
                "run",
                "stasis",
                "grant",
                agent_id,
                "--symptoms",
                "tool_scope_violation",
                "--duration",
                "1h",
                "--reason",
                "DoD step 9: grant that will be bypassed",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=op_env,
            timeout=30.0,
            check=False,
        )
        if grant_proc.returncode != 0:
            print(grant_proc.stdout)
            print(grant_proc.stderr, file=sys.stderr)
            _fail(f"`stasis grant` exited {grant_proc.returncode}")
        _ok("grant issued")

        # Step 2: kill the agent. The grant must not save it.
        print("  issuing manual kill (should bypass the grant)…")
        kill_proc = subprocess.run(
            [
                "uv",
                "run",
                "stasis",
                "kill",
                agent_id,
                "--reason",
                "DoD step 9: manual override",
                "--poll-interval",
                "0.5",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=op_env,
            timeout=120.0,
            check=False,
        )
        if kill_proc.returncode != 0:
            print(kill_proc.stdout)
            print(kill_proc.stderr, file=sys.stderr)
            _fail(f"`stasis kill` exited {kill_proc.returncode}")
        if "confirmed dead" not in kill_proc.stdout:
            _fail("`stasis kill` did not confirm death")
        _ok("CLI reported confirmed dead despite active grant")

        try:
            agent_proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            agent_proc.kill()
            _fail("idle agent did not exit within 30s after kill")
        if agent_proc.returncode != 3:
            _fail(
                f"agent exited {agent_proc.returncode}, expected 3 "
                f"(StasisTerminated)"
            )
        _ok("agent process exited 3")

        # Step 3: verify the kill_event is manual + the grant is still
        # there (not auto-revoked — grants survive their bypass).
        async def _verify() -> None:
            async with StasisClient.from_config() as client:
                kills = await client.list_kill_events(agent_id)
                if not kills:
                    _fail("no kill_events found")
                ke = kills[0]
                if ke.trigger_type.value != "manual":
                    _fail(
                        f"trigger_type={ke.trigger_type.value} (expected manual)"
                    )
                _ok("kill_event.trigger_type = manual")
                grants = await client.list_grants(agent_id)
                if not grants:
                    _fail("grant disappeared (shouldn't be auto-revoked)")
                g = grants[0]
                # The grant may still be ACTIVE (not revoked, not expired)
                # — it just never had a chance to suppress anything because
                # manual kill bypasses the apply_grants path. The fact that
                # it's still here in the DB is the audit point.
                _ok(f"grant still recorded id={g.id} active={g.active}")

        asyncio.run(_verify())
    finally:
        if agent_proc.poll() is None:
            agent_proc.kill()
            agent_proc.wait(timeout=5.0)


# --- helpers -------------------------------------------------------------


def _scrape_agent_id(text: str) -> str | None:
    """The demo prints `uv run stasis logs <UUID>` — pull the UUID out."""
    import re

    m = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        text,
    )
    return m.group(0) if m else None


# --- entry point ---------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the DoD demo. Steps 1-4 implemented today (M2.6).",
    )
    parser.add_argument(
        "--skip-step",
        default="",
        help="comma-separated step numbers to skip, e.g. '2,3'",
    )
    parser.add_argument(
        "--check-control-plane",
        action="store_true",
        help="ping STASIS_BASE_URL/healthz before starting and fail if unreachable",
    )
    args = parser.parse_args()

    _load_env()

    skip = set()
    for s in args.skip_step.split(","):
        s = s.strip()
        if s.isdigit():
            skip.add(int(s))

    if args.check_control_plane:
        _preflight_health_check()

    healthy_agent: str | None = None
    induced_agent: str | None = None

    if 1 not in skip:
        healthy_agent = step_1_integration()
        # Give the BackgroundWorker one tick so the events from step 1 actually
        # reach the server before step 2 reads them back.
        time.sleep(2.0)
    if 2 not in skip:
        if not healthy_agent:
            _fail("step 2 needs an agent_id from step 1 (don't skip step 1)")
        assert healthy_agent is not None
        step_2_logs(healthy_agent)
    if 3 not in skip:
        induced_agent = step_3_loop_induction()
        time.sleep(2.0)
    if 4 not in skip:
        if not induced_agent:
            _fail("step 4 needs an agent_id from step 3 (don't skip step 3)")
        assert induced_agent is not None
        step_4_death_certificate(induced_agent)

    if 7 not in skip:
        step_7_manual_kill()
    if 8 not in skip:
        step_8_grant()
    if 9 not in skip:
        step_9_manual_kill_bypasses_grant()

    print()
    print("=" * 70)
    print("  DoD steps 1-4 + 7-9 PASSED.")
    print("  Step 6 (feedback) shipped in M3 but isn't wired into this script yet.")
    print("  Step 5 (webhooks) deferred post-MVP.")
    print("=" * 70)


def _preflight_health_check() -> None:
    import urllib.error
    import urllib.request

    base = os.environ.get("STASIS_BASE_URL", "http://localhost:8000")
    url = base.rstrip("/") + "/healthz"
    print(f"  preflight: GET {url}")
    try:
        with urllib.request.urlopen(url, timeout=5.0) as r:
            if r.status >= 400:
                _fail(f"control plane unhealthy: HTTP {r.status}")
    except urllib.error.URLError as exc:
        _fail(f"control plane unreachable at {url}: {exc}")
    _ok("control plane reachable")


if __name__ == "__main__":
    main()
