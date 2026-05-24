"""CLI helper functions — display, input parsing, async runner."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stasis_agent.client import AuthError, NotFoundError, StasisClient, TransportError
from stasis_agent.policies import UnknownPolicyError, resolve_policy
from stasis_agent.types import AgentStatus, AgentSummary, EventOut, EventType, SymptomType

console = Console()
err_console = Console(stderr=True)


def _run(coro: Any) -> None:
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
            console.print(
                f"[dim]{ts}[/dim] [yellow]lifecycle[/yellow] {phase} [dim]{extra}[/dim]"
            )
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


def _worst_case_latency(policy_name: str) -> float:
    """Sum the three windows in `policy.thresholds` that bound manual-kill latency.
    Unknown policy → conservative fallback (~43s)."""
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
