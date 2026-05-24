"""`stasis` CLI entry point (Typer + Rich).

Commands so far:

    stasis fleet
    stasis logs <agent_id> [--follow] [--limit N]
    stasis kill <agent_id> --reason "..."        # M4

Commands stubbed for later milestones:

    stasis grant <agent_id> --symptoms ...       # M5
    stasis revoke <grant_id> --reason "..."      # M5
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Coroutine
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stasis_agent._version import __version__
from stasis_agent.client import (
    AuthError,
    ConflictError,
    NotFoundError,
    StasisClient,
    TransportError,
)
from stasis_agent.exceptions import StasisError
from stasis_agent.policies import UnknownPolicyError, resolve_policy
from stasis_agent.types import (
    AgentStatus,
    AgentSummary,
    EventOut,
    EventType,
    SymptomType,
)

app = typer.Typer(
    name="stasis",
    help="Stasis CLI — agent supervision via the apoptosis protocol.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    _ = version


# --- shared error handling ----------------------------------------------


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run an async CLI body and translate SDK exceptions into clean CLI errors."""
    try:
        asyncio.run(coro)
    except AuthError as exc:
        err_console.print(f"[red]auth error:[/red] {exc}")
        err_console.print(
            "[dim]Set STASIS_API_KEY in your environment or .env file.[/dim]"
        )
        raise typer.Exit(2) from exc
    except NotFoundError as exc:
        err_console.print(f"[red]not found:[/red] {exc}")
        raise typer.Exit(4) from exc
    except TransportError as exc:
        err_console.print(f"[red]cannot reach control plane:[/red] {exc}")
        err_console.print(
            "[dim]Is the server running? `uv run stasis-control-plane`[/dim]"
        )
        raise typer.Exit(5) from exc


# --- fleet ---------------------------------------------------------------


@app.command()
def fleet() -> None:
    """List registered agents and their statuses."""
    _run(_fleet())


async def _fleet() -> None:
    async with StasisClient.from_config() as client:
        agents = await client.list_agents()
    _render_fleet(agents)


def _render_fleet(agents: list[AgentSummary]) -> None:
    if not agents:
        console.print("[dim]no agents registered[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Policy", style="magenta")
    table.add_column("Status", style="yellow")
    table.add_column("Last HB", justify="right")
    table.add_column("Registered", justify="right")
    for a in agents:
        last_hb = a.last_heartbeat_at.strftime("%H:%M:%S") if a.last_heartbeat_at else "-"
        reg = a.registered_at.strftime("%H:%M:%S")
        table.add_row(
            str(a.id),
            a.name,
            a.policy_name,
            a.status.value,
            last_hb,
            reg,
        )
    console.print(table)


# --- logs ----------------------------------------------------------------


@app.command()
def logs(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `stasis fleet`."),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Tail events as they arrive (Ctrl+C to stop)."
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=1000),
    interval: float = typer.Option(
        1.0, "--interval", help="Poll interval in seconds when --follow."
    ),
) -> None:
    """Show events for an agent. Use --follow to tail live."""
    _run(_logs(agent_id, follow=follow, limit=limit, interval=interval))


async def _logs(agent_id: str, *, follow: bool, limit: int, interval: float) -> None:
    async with StasisClient.from_config() as client:
        # Initial page is descending (most recent first); reverse for display
        # so the oldest line in the screen-full is at the top.
        page = await client.list_events(agent_id, limit=limit)
        for ev in reversed(page.events):
            _print_event(ev)

        if not follow:
            return

        last_id = page.last_id or 0
        with contextlib.suppress(KeyboardInterrupt):
            while True:
                await asyncio.sleep(interval)
                page = await client.list_events(agent_id, after_id=last_id, limit=500)
                for ev in page.events:  # already ascending in tail mode
                    _print_event(ev)
                if page.last_id is not None:
                    last_id = page.last_id


def _print_event(ev: EventOut) -> None:
    ts = ev.created_at.strftime("%H:%M:%S")
    match ev.type:
        case EventType.TOOL_CALL:
            tool = ev.payload.get("tool", "?")
            console.print(f"[dim]{ts}[/dim] [cyan]tool[/cyan]      {tool}")
        case EventType.LLM_CALL:
            model = ev.payload.get("model", "?")
            in_tok = ev.payload.get("input_tokens", 0)
            out_tok = ev.payload.get("output_tokens", 0)
            cost = ev.payload.get("cost_usd", 0.0)
            console.print(
                f"[dim]{ts}[/dim] [magenta]llm[/magenta]       "
                f"{model} in={in_tok} out={out_tok} [green]${cost:.4f}[/green]"
            )
        case EventType.LIFECYCLE:
            phase = ev.payload.get("phase", "?")
            extra = " ".join(f"{k}={v}" for k, v in ev.payload.items() if k != "phase")
            console.print(f"[dim]{ts}[/dim] [yellow]lifecycle[/yellow] {phase} [dim]{extra}[/dim]")
        case EventType.HEARTBEAT:
            up = ev.payload.get("uptime_seconds", 0.0)
            console.print(f"[dim]{ts}[/dim] [green]heartbeat[/green] up={up:.1f}s")
        case EventType.SYMPTOM:
            stype = ev.payload.get("symptom_type", "?")
            sev = ev.payload.get("severity", "?")
            color = "red" if sev == "terminal" else "yellow"
            console.print(f"[dim]{ts}[/dim] [{color}]symptom[/{color}]   {stype} ({sev})")
        case _:
            console.print(f"[dim]{ts}[/dim] {ev.type.value} {ev.payload}")


# --- placeholders (later milestones) -------------------------------------


@app.command()
def kill(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `stasis fleet`."),
    reason: str = typer.Option(
        ..., "--reason", help="Operator justification. Persisted on the death cert."
    ),
    poll_interval: float = typer.Option(
        0.5,
        "--poll-interval",
        help="How often (s) the CLI polls for kill progress. CLI-side only.",
        min=0.1,
        max=10.0,
    ),
) -> None:
    """Manually terminate an agent.

    Issues `POST /agents/{id}/terminate` and then polls the control plane
    for staged progress: issue → cooperative wait → cert confirmed.

    Prints the policy's worst-case latency budget upfront so the user
    isn't surprised by the wait. Times out at 2x worst-case and reports
    "kill issued but unconfirmed" — see TODO #4 for the UX motivation.
    """
    _run(_kill(agent_id, reason=reason, poll_interval=poll_interval))


async def _kill(agent_id: str, *, reason: str, poll_interval: float) -> None:
    async with StasisClient.from_config() as client:
        # Step 0: fetch the agent + its policy so we can print the
        # worst-case latency budget. Two extra round trips before kill,
        # but the UX win is worth it — the user types `stasis kill` and
        # immediately knows "this might take ~43s." See TODO #4.
        try:
            agent = await client.get_agent(agent_id)
        except NotFoundError:
            err_console.print(f"[red]not found:[/red] {agent_id}")
            raise typer.Exit(4) from None

        worst_case_seconds = _worst_case_latency(agent.policy_name)
        cli_timeout_seconds = max(60.0, worst_case_seconds * 2)

        console.print(
            f"[dim]policy={agent.policy_name}; worst-case cooperative kill "
            f"latency = {worst_case_seconds:.0f}s "
            f"(poll + grace + verification). CLI timeout: "
            f"{cli_timeout_seconds:.0f}s.[/dim]"
        )

        # Step 1: issue the kill.
        try:
            result = await client.terminate_agent(agent_id, reason=reason)
        except ConflictError as exc:
            err_console.print(f"[yellow]already dying:[/yellow] {exc}")
            raise typer.Exit(3) from exc

        if isinstance(result, int):
            # 409 → the partial-unique race; treat as success-ish.
            console.print(
                f"[yellow]✓[/yellow] kill already in flight "
                f"(existing kill_event={result})"
            )
            kill_event_id = result
        else:
            console.print(
                f"[green]✓[/green] kill issued (kill_event={result.id})"
            )
            kill_event_id = result.id

        # Step 2: watch the agent transition. The CLI polls every
        # `poll_interval` for display; this is independent of the SDK
        # poller's `kill_poll_interval_seconds` (which is what *the
        # agent process* uses to pick up the kill).
        await _watch_kill(
            client,
            agent_id,
            kill_event_id=kill_event_id,
            poll_interval=poll_interval,
            timeout_seconds=cli_timeout_seconds,
        )


def _worst_case_latency(policy_name: str) -> float:
    """Sum the three windows in `policy.thresholds` that bound manual-kill
    latency. Unknown policy → conservative fallback (~43s)."""
    try:
        policy = resolve_policy(policy_name)
    except UnknownPolicyError:
        return 43.0
    t = policy.thresholds
    # No per-policy kill_poll_interval yet (TODO #4) — use the SDK default.
    from stasis_agent.watcher import DEFAULT_KILL_POLL_INTERVAL

    return float(
        DEFAULT_KILL_POLL_INTERVAL
        + t.cooperative_grace_seconds
        + t.verification_timeout_seconds
    )


async def _watch_kill(
    client: StasisClient,
    agent_id: str,
    *,
    kill_event_id: int,
    poll_interval: float,
    timeout_seconds: float,
) -> None:
    """Poll the agent until terminal or the wall clock runs out."""
    start = time.monotonic()
    last_status: AgentStatus | None = None
    cooperative_announced = False

    while True:
        try:
            agent = await client.get_agent(agent_id)
        except NotFoundError:
            err_console.print("[red]agent disappeared during kill[/red]")
            raise typer.Exit(4) from None

        elapsed = time.monotonic() - start

        if agent.status != last_status:
            last_status = agent.status

        if agent.status == AgentStatus.DYING and not cooperative_announced:
            console.print(
                "[dim]… agent acknowledged kill; running cooperative "
                "shutdown[/dim]"
            )
            cooperative_announced = True

        if agent.status == AgentStatus.TERMINATED:
            console.print(
                f"[green]✓[/green] confirmed dead "
                f"([dim]elapsed {elapsed:.1f}s[/dim])"
            )
            return

        if elapsed >= timeout_seconds:
            err_console.print(
                f"[yellow]…[/yellow] kill issued but unconfirmed within "
                f"{timeout_seconds:.0f}s. "
                f"Agent may be a zombie (SDK didn't cooperate). Check "
                f"`stasis logs {agent_id}` or `stasis fleet`. "
                f"kill_event={kill_event_id}"
            )
            raise typer.Exit(6)

        await asyncio.sleep(poll_interval)


@app.command()
def grant(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `stasis fleet`."),
    symptoms: str = typer.Option(
        ...,
        "--symptoms",
        help="Comma-separated symptom names (loop, tool_scope_violation, …).",
    ),
    duration: str = typer.Option(
        ...,
        "--duration",
        help="How long the grant is valid. Format: 30m, 2h, 600s.",
    ),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    """Issue an apoptosis-proofing grant for an agent.

    The agent's policy gates which symptoms are grantable; the server
    422s on policy violations. `manual_kill` is never grantable.
    """
    _run(_grant(agent_id, symptoms=symptoms, duration=duration, reason=reason))


async def _grant(
    agent_id: str, *, symptoms: str, duration: str, reason: str
) -> None:
    parsed_symptoms = _parse_symptoms(symptoms)
    duration_seconds = _parse_duration(duration)

    async with StasisClient.from_config() as client:
        try:
            grant = await client.create_grant(
                agent_id,
                symptoms=parsed_symptoms,
                duration_seconds=duration_seconds,
                reason=reason,
            )
        except StasisError as exc:
            err_console.print(f"[red]grant rejected:[/red] {exc}")
            raise typer.Exit(7) from exc

    console.print(f"[green]✓[/green] grant issued (id={grant.id})")
    console.print(
        f"  [dim]symptoms:[/dim] "
        f"{', '.join(s.value for s in grant.symptoms)}"
    )
    console.print(f"  [dim]expires: [/dim] {grant.expires_at.isoformat()}")
    console.print(f"  [dim]reason:  [/dim] {grant.reason}")


@app.command()
def revoke(
    grant_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    """Revoke an active apoptosis-proofing grant. Idempotent."""
    _run(_revoke(grant_id, reason=reason))


async def _revoke(grant_id: str, *, reason: str) -> None:
    async with StasisClient.from_config() as client:
        grant = await client.revoke_grant(grant_id, reason=reason)
    if grant.revoked_at is not None:
        console.print(
            f"[green]✓[/green] grant revoked "
            f"([dim]at {grant.revoked_at.isoformat()}[/dim])"
        )
    else:
        # Idempotent path — server returned the unchanged row.
        console.print("[yellow]…[/yellow] grant was already revoked")


def _parse_symptoms(raw: str) -> list[SymptomType]:
    out: list[SymptomType] = []
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        try:
            out.append(SymptomType(name))
        except ValueError as exc:
            valid = ", ".join(s.value for s in SymptomType)
            raise typer.BadParameter(
                f"unknown symptom {name!r}; valid: {valid}"
            ) from exc
    if not out:
        raise typer.BadParameter("--symptoms cannot be empty")
    return out


def _parse_duration(raw: str) -> int:
    """Accept 30s, 5m, 2h. Returns seconds. Caps at 24h to match server."""
    raw = raw.strip().lower()
    if not raw:
        raise typer.BadParameter("--duration cannot be empty")
    unit_seconds = {"s": 1, "m": 60, "h": 3600}
    if raw[-1] in unit_seconds:
        try:
            n = int(raw[:-1])
        except ValueError as exc:
            raise typer.BadParameter(
                f"bad --duration {raw!r}; use e.g. 30m, 2h, 600s"
            ) from exc
        seconds = n * unit_seconds[raw[-1]]
    else:
        try:
            seconds = int(raw)
        except ValueError as exc:
            raise typer.BadParameter(
                f"bad --duration {raw!r}; use e.g. 30m, 2h, 600s"
            ) from exc
    if seconds < 60:
        raise typer.BadParameter("--duration must be at least 60s")
    if seconds > 86_400:
        raise typer.BadParameter("--duration cannot exceed 24h (86400s)")
    return seconds


policies_app = typer.Typer(help="Manage supervision policies (M5).")
app.add_typer(policies_app, name="policies")


@policies_app.command("list")
def policies_list() -> None:
    typer.echo("policies list: not yet implemented (lands in M5)", err=True)
    raise typer.Exit(1)


if __name__ == "__main__":
    app()


# Re-export for tests that want to inspect the Typer app.
__all__ = ["_fleet", "_logs", "_print_event", "_render_fleet", "app"]
