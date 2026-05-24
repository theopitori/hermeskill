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

import typer

from stasis_agent._version import __version__
from stasis_agent.cli_helpers import (
    _parse_duration,
    _parse_symptoms,
    _print_event,
    _render_fleet,
    _run,
    _watch_kill,
    _worst_case_latency,
    console,
    err_console,
)
from stasis_agent.client import (
    ConflictError,
    NotFoundError,
    StasisClient,
)
from stasis_agent.exceptions import StasisError

app = typer.Typer(
    name="stasis",
    help="Stasis CLI — agent supervision via the apoptosis protocol.",
    no_args_is_help=True,
)


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


# --- fleet ---------------------------------------------------------------


@app.command()
def fleet() -> None:
    """List registered agents and their statuses."""
    _run(_fleet())


async def _fleet() -> None:
    async with StasisClient.from_config() as client:
        agents = await client.list_agents()
    _render_fleet(agents)


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


# --- kill ----------------------------------------------------------------


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


# --- grant ---------------------------------------------------------------


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


# --- revoke --------------------------------------------------------------


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


# --- placeholders (later milestones) -------------------------------------


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
