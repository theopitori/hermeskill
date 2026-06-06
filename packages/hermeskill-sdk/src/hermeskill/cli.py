"""`hermeskill` CLI entry point (Typer + Rich).

Commands so far:

    hermeskill fleet
    hermeskill logs <agent_id> [--follow] [--limit N]
    hermeskill kill <agent_id> --reason "..."        # M4

Commands stubbed for later milestones:

    hermeskill grant <agent_id> --symptoms ...       # M5
    hermeskill revoke <grant_id> --reason "..."      # M5
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from datetime import UTC, datetime
from uuid import UUID

import typer
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermeskill._version import __version__
from hermeskill.client import (
    AuthError,
    ConflictError,
    HermeskillClient,
    NotFoundError,
    TransportError,
)
from hermeskill.config import CONFIG_PATH, DEFAULT_BASE_URL, SDKConfig, save_config
from hermeskill.exceptions import HermeskillError
from hermeskill.policies import UnknownPolicyError, resolve_policy
from hermeskill.types import (
    AgentStatus,
    AgentSummary,
    EventOut,
    EventType,
    SymptomType,
)
from hermeskill.vitals import (
    DEFAULT_MAX_AGE_SECONDS,
    VitalsSnapshot,
    iter_live_snapshots,
    read_snapshot,
    snapshot_path,
)

# Windows consoles default to the cp1252 code page, which cannot encode the
# Unicode glyphs (→, —, ✓, ⊘) used in our help text and Rich tables. Without
# this, `hermeskill kill --help` and friends crash with UnicodeEncodeError and
# `hermeskill fleet` prints replacement chars. reconfigure() exists on
# TextIOWrapper (Python 3.7+); guard it so non-standard streams are left alone.
# Must run before the Rich Console objects below capture sys.stdout.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

app = typer.Typer(
    name="hermeskill",
    help="Hermeskill CLI — agent supervision via the apoptosis protocol.",
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


# --- init ----------------------------------------------------------------


@app.command()
def init(
    api_key: str = typer.Option(
        ...,
        "--api-key",
        help="Your Hermeskill API key. For `kill`/`rm`/`prune` use an "
        "operator-role key (it also works for read commands + agents).",
    ),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL, "--base-url", help="Control plane URL."
    ),
    policy: str | None = typer.Option(
        None, "--policy", help="Default policy name for watched agents."
    ),
    agent_name: str | None = typer.Option(
        None, "--agent-name", help="Default display name for this agent."
    ),
    local_cert: bool = typer.Option(
        True,
        "--local-cert/--no-local-cert",
        help="Print + save the death cert locally on a kill (default: on).",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing config file."
    ),
) -> None:
    """Write ~/.hermeskill/config.toml so you don't set env vars every session.

    Resolution order is unchanged (env vars still override the file), but once
    written the SDK and CLI read the file from your home directory — so they
    work from any directory, not just inside the repo.
    """
    config = SDKConfig(
        base_url=base_url,
        api_key=api_key,
        policy=policy,
        agent_name=agent_name,
        local_cert=local_cert,
    )
    try:
        path = save_config(config, force=force)
    except FileExistsError as exc:
        err_console.print(
            f"[yellow]config already exists:[/yellow] {exc}\n"
            "[dim]Re-run with --force to overwrite it.[/dim]"
        )
        raise typer.Exit(3) from exc
    console.print(f"[green]✓[/green] wrote {path}")
    console.print(
        "[dim]env vars (HERMESKILL_API_KEY, …) still override this file when set.[/dim]"
    )


# --- enable-hermes -------------------------------------------------------


@app.command("enable-hermes")
def enable_hermes(
    disable: bool = typer.Option(
        False, "--disable", help="Remove hermeskill from plugins.enabled instead."
    ),
) -> None:
    """Enable the Hermeskill plugin in your Hermes config (one-shot).

    Adds (or, with --disable, removes) ``hermeskill`` to ``plugins.enabled`` in
    your Hermes config. This is the supported enable path for pip/entry-point
    plugins — ``hermes plugins enable`` only manages git-installed plugins and
    won't see Hermeskill.
    """
    try:
        from hermes_cli.config import (  # type: ignore[import-untyped]
            get_config_path,
            load_config,
            save_config,
        )
    except ImportError:
        err_console.print(
            "[red]Hermes Agent isn't importable from this environment.[/red]\n"
            "[dim]Install the plugin into Hermes' venv, e.g.:\n"
            "  uv tool install hermes-agent --with hermeskill-hermes\n"
            "then run `hermeskill enable-hermes` from there (or edit "
            "plugins.enabled in your Hermes config by hand).[/dim]"
        )
        raise typer.Exit(2) from None

    cfg = load_config()
    plugins = cfg.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        err_console.print("[red]plugins.enabled is not a list in your Hermes config.[/red]")
        raise typer.Exit(1)

    path = get_config_path()
    if disable:
        if "hermeskill" not in enabled:
            console.print("[dim]hermeskill was not enabled — nothing to do.[/dim]")
            return
        plugins["enabled"] = [p for p in enabled if p != "hermeskill"]
        save_config(cfg)
        console.print(f"[green]✓[/green] removed hermeskill from plugins.enabled ({path})")
        return

    if "hermeskill" in enabled:
        console.print(f"[dim]hermeskill already enabled ({path}).[/dim]")
        return
    enabled.append("hermeskill")
    save_config(cfg)
    console.print(f"[green]✓[/green] enabled hermeskill in {path}")
    console.print("[dim]Run Hermes and Hermeskill supervises every session.[/dim]")


# --- doctor --------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Diagnose the Hermeskill wiring and print what supervision will do.

    Read-only — touches no network and changes no files. Reports, with ✓/✗:

      • whether Hermes Agent is importable from this environment;
      • whether the Hermeskill plugin packages are importable;
      • whether ``hermeskill`` is listed in your Hermes ``plugins.enabled``;
      • the resolved SDK config + mode (control-plane vs local-only/keyless),
        policy, agent name, and local-cert setting.

    Exit code is non-zero if a *blocking* problem is found (Hermes missing, or
    the plugin not enabled) — so it's usable as a smoke check in scripts.
    """
    problems = 0

    console.print("[bold]Hermeskill doctor[/bold]")

    # 1. Hermes importable? (same probe enable-hermes uses.)
    hermes_config_path: object | None = None
    hermes_enabled_list: list[object] | None = None
    try:
        from hermes_cli.config import (  # type: ignore[import-untyped, unused-ignore]
            get_config_path,
            load_config,
        )

        console.print("  [green]✓[/green] Hermes Agent is importable")
        try:
            hermes_config_path = get_config_path()
            cfg = load_config()
            plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
            enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
            if isinstance(enabled, list):
                hermes_enabled_list = list(enabled)
        except Exception as exc:  # diagnostics must never crash
            console.print(
                f"  [yellow]…[/yellow] could not read Hermes config: {exc}"
            )
    except ImportError:
        problems += 1
        console.print(
            "  [red]✗[/red] Hermes Agent is NOT importable from this "
            "environment\n"
            "      [dim]install it alongside the plugin, e.g.:\n"
            "        uv tool install hermes-agent --with hermeskill-hermes[/dim]"
        )

    # 2. Plugin packages importable?
    try:
        import hermeskill_hermes  # type: ignore[import-not-found, unused-ignore]  # noqa: F401

        console.print("  [green]✓[/green] hermeskill-hermes plugin is importable")
    except ImportError:
        problems += 1
        console.print(
            "  [red]✗[/red] hermeskill-hermes is NOT importable\n"
            "      [dim]install it into the same environment as Hermes.[/dim]"
        )

    # 3. Plugin enabled in the Hermes config?
    if hermes_config_path is not None:
        console.print(f"  [dim]Hermes config:[/dim] {hermes_config_path}")
        if hermes_enabled_list is None:
            console.print(
                "  [yellow]…[/yellow] plugins.enabled is missing or not a list "
                "in your Hermes config"
            )
        elif "hermeskill" in hermes_enabled_list:
            console.print(
                "  [green]✓[/green] hermeskill is enabled in plugins.enabled"
            )
        else:
            problems += 1
            console.print(
                "  [red]✗[/red] hermeskill is NOT in plugins.enabled\n"
                "      [dim]run `hermeskill enable-hermes` to add it.[/dim]"
            )

    # 4. Resolved SDK config + mode.
    config = SDKConfig.load()
    console.print(f"  [dim]SDK config:[/dim] {CONFIG_PATH}"
                  + ("" if CONFIG_PATH.exists() else " [dim](not written yet)[/dim]"))
    if config.api_key:
        console.print(
            f"  [green]✓[/green] mode: control-plane "
            f"[dim](base_url={config.base_url})[/dim]"
        )
    else:
        console.print(
            "  [yellow]●[/yellow] mode: local-only (keyless) — in-process "
            "supervision + local death certs; no fleet view / remote kill / "
            "grants\n"
            "      [dim]set HERMESKILL_API_KEY (or run `hermeskill init`) to "
            "attach a control plane.[/dim]"
        )
    console.print(f"  [dim]policy:[/dim]     {config.policy or 'coding-default (adapter default)'}")
    console.print(f"  [dim]agent name:[/dim] {config.agent_name or '(adapter default)'}")
    console.print(f"  [dim]local cert:[/dim] {'on' if config.local_cert else 'off'}")

    if problems:
        console.print(
            f"\n[red]{problems} blocking problem(s) found.[/red] "
            "[dim]Supervision will not run until these are fixed.[/dim]"
        )
        raise typer.Exit(1)
    console.print("\n[green]✓ wiring looks good.[/green]")


# --- shared error handling ----------------------------------------------


def _run(coro: object) -> None:
    """Run an async CLI body and translate SDK exceptions into clean CLI errors."""
    try:
        asyncio.run(coro)  # type: ignore[arg-type]
    except AuthError as exc:
        err_console.print(f"[red]auth error:[/red] {exc}")
        err_console.print(
            "[dim]Set HERMESKILL_API_KEY in your environment or .env file.[/dim]"
        )
        raise typer.Exit(2) from exc
    except NotFoundError as exc:
        err_console.print(f"[red]not found:[/red] {exc}")
        raise typer.Exit(4) from exc
    except TransportError as exc:
        err_console.print(f"[red]cannot reach control plane:[/red] {exc}")
        err_console.print(
            "[dim]Is the server running? `uv run hermeskill-control-plane`[/dim]"
        )
        raise typer.Exit(5) from exc


# --- fleet ---------------------------------------------------------------


# Statuses hidden from the default fleet view — terminal states that
# otherwise pile up forever. `--all` shows them; `--status` targets one.
_INACTIVE_STATUSES = frozenset({AgentStatus.TERMINATED, AgentStatus.ZOMBIE})


@app.command()
def fleet(
    all_: bool = typer.Option(
        False, "--all", "-a", help="Include terminated/zombie agents."
    ),
    status: str | None = typer.Option(
        None, "--status", help="Show only agents in this status (e.g. terminated)."
    ),
) -> None:
    """List agents. Defaults to active only; use --all or --status to widen."""
    _run(_fleet(all_=all_, status=status))


async def _fleet(*, all_: bool, status: str | None) -> None:
    async with HermeskillClient.from_config() as client:
        agents = await client.list_agents(status=status)
    if status is None and not all_:
        agents = [a for a in agents if a.status not in _INACTIVE_STATUSES]
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


# --- rm / prune ----------------------------------------------------------


@app.command()
def rm(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `hermeskill fleet --all`."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Delete an agent and its history (events, kill events, grants).

    Destructive and irreversible — requires an operator-role key. Use this to
    stop old agents piling up in the fleet.
    """
    if not yes:
        typer.confirm(
            f"Permanently delete agent {agent_id} and all its history?", abort=True
        )
    _run(_rm(agent_id))


async def _rm(agent_id: str) -> None:
    async with HermeskillClient.from_config() as client:
        await client.delete_agent(agent_id)
    console.print(f"[green]✓[/green] deleted agent {agent_id}")


@app.command()
def prune(
    status: str = typer.Option(
        "terminated", "--status", help="Delete all your agents in this status."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Bulk-delete agents in a terminal status (default: terminated).

    Destructive and irreversible — requires an operator-role key. Scoped to
    your own agents only.
    """
    if not yes:
        typer.confirm(
            f"Permanently delete ALL '{status}' agents and their history?", abort=True
        )
    _run(_prune(status))


async def _prune(status: str) -> None:
    async with HermeskillClient.from_config() as client:
        deleted = await client.prune_agents(status=status)
    console.print(f"[green]✓[/green] pruned {deleted} agent(s) in status '{status}'")


# --- logs ----------------------------------------------------------------


@app.command()
def logs(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `hermeskill fleet`."),
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
    async with HermeskillClient.from_config() as client:
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
            # Symptom events carry the type under "symptom" (see
            # WatcherState.record_symptom); tolerate the legacy "symptom_type"
            # key so older event rows still render.
            stype = ev.payload.get("symptom") or ev.payload.get("symptom_type") or "?"
            sev = ev.payload.get("severity", "?")
            reason = ev.payload.get("reason", "")
            color = "red" if sev == "terminal" else "yellow"
            suffix = f" [dim]{reason}[/dim]" if reason else ""
            console.print(f"[dim]{ts}[/dim] [{color}]symptom[/{color}]   {stype} ({sev}){suffix}")
        case _:
            console.print(f"[dim]{ts}[/dim] {ev.type.value} {ev.payload}")


# --- monitor (live vitals; keyless, file-backed) -------------------------

_PULSE_WIDTH = 28


@app.command()
def monitor(
    agent_id: str | None = typer.Argument(
        None, help="Agent UUID to watch. Omit to follow the freshest live agent."
    ),
    interval: float = typer.Option(
        0.5, "--interval", min=0.05, max=5.0, help="Repaint interval in seconds."
    ),
    once: bool = typer.Option(
        False, "--once", help="Render a single frame and exit (no live loop)."
    ),
) -> None:
    """Live agent vitals — the real-time counterpart to the death certificate.

    Reads the local vitals file the Hermes plugin writes each tick
    (``~/.hermeskill/live/``). Works with **no control plane and no API key** —
    the keyless sibling of ``hermeskill logs --follow``. Run it in a second
    terminal beside ``hermes chat``: watch cost climb and loop pressure build,
    then — the instant apoptosis fires — the panel goes red and flatlines.

    Ctrl+C to stop.
    """
    target: UUID | None = None
    if agent_id is not None:
        try:
            target = UUID(agent_id)
        except ValueError as exc:
            raise typer.BadParameter(
                f"not a valid agent UUID: {agent_id!r}"
            ) from exc

    if once:
        console.print(_render_vitals(_load_snapshot(target)))
        return

    # Anchor the session at launch. In auto-follow (no id) we ignore deaths
    # that happened *before* we started watching — otherwise a leftover
    # terminated file from a previous run would render a flatline and the loop
    # would exit instantly, before the agent we actually want to watch starts.
    since = datetime.now(UTC)
    with (
        Live(
            _render_vitals(_load_snapshot(target, since=since)),
            console=console,
            refresh_per_second=4,
            screen=False,
        ) as live,
        contextlib.suppress(KeyboardInterrupt),
    ):
        while True:
            snap = _load_snapshot(target, since=since)
            live.update(_render_vitals(snap))
            # A terminal snapshot is the last word — freeze the final frame
            # (flatline / ended) on screen and stop polling.
            if snap is not None and snap.status in ("terminated", "ended_clean"):
                break
            time.sleep(interval)


def _load_snapshot(
    target: UUID | None, *, since: datetime | None = None
) -> VitalsSnapshot | None:
    """Read the watched agent's snapshot, or the freshest live one if no id.

    With an explicit ``target``, the caller asked for a specific agent — return
    its snapshot whatever its state. In auto mode (no id), ``since`` filters out
    terminal snapshots that predate the watch so a stale corpse from a previous
    run can't hijack the pane.
    """
    if target is not None:
        return read_snapshot(snapshot_path(target))
    snaps = iter_live_snapshots()
    if since is not None:
        snaps = [
            s
            for s in snaps
            if not (
                s.status in ("terminated", "ended_clean") and s.written_at < since
            )
        ]
    return snaps[0] if snaps else None


def _mode(snap: VitalsSnapshot) -> str:
    return "local-only" if snap.offline else "control-plane"


def _bar(ratio: float, width: int = 24) -> tuple[str, str]:
    """A proportional bar + a colour that reddens as the ratio nears the cap."""
    ratio = max(0.0, min(1.0, ratio))
    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    if ratio >= 0.85:
        color = "red"
    elif ratio >= 0.6:
        color = "yellow"
    else:
        color = "green"
    return bar, color


def _gauge(label: str, ratio: float, value: str) -> Text:
    bar, color = _bar(ratio)
    t = Text()
    t.append(f"{label:<6} ", style="dim")
    t.append(bar, style=color)
    t.append(f"  {value}", style=color)
    return t


def _pulse(color: str) -> Text:
    """A moving spike over a baseline — the ECG line while the agent is alive."""
    pos = int(time.monotonic() * 8) % _PULSE_WIDTH
    chars = ["─"] * _PULSE_WIDTH
    chars[pos] = "╿"
    return Text("".join(chars), style=color)


def _render_vitals(snap: VitalsSnapshot | None) -> RenderableType:
    now = datetime.now(UTC)
    if snap is None:
        return Panel(
            Text(
                "waiting for a live agent…\n"
                "start `hermes chat` with the hermeskill plugin enabled.",
                style="dim",
            ),
            title="hermeskill monitor",
            border_style="dim",
        )
    if snap.status == "terminated":
        return _render_flatline(snap)
    if snap.status == "ended_clean":
        return _render_ended(snap)
    age = (now - snap.written_at).total_seconds()
    if age > DEFAULT_MAX_AGE_SECONDS:
        return _render_no_signal(snap, age)
    return _render_running(snap, age)


def _render_running(snap: VitalsSnapshot, age: float) -> RenderableType:
    # Extrapolate uptime from the last write so the clock ticks smoothly
    # between agent actions and the time gauge lines up with check_wall_clock.
    uptime = snap.uptime_seconds + max(0.0, age)
    cost_ratio = (
        snap.total_cost_usd / snap.max_cost_usd if snap.max_cost_usd > 0 else 0.0
    )
    loop_ratio = (
        snap.loop_peak / snap.max_loop_repeats if snap.max_loop_repeats > 0 else 0.0
    )
    time_ratio = (
        uptime / snap.max_runtime_seconds if snap.max_runtime_seconds > 0 else 0.0
    )
    tokens = snap.total_input_tokens + snap.total_output_tokens
    body = Group(
        _pulse("green"),
        Text(""),
        _gauge(
            "cost",
            cost_ratio,
            f"${snap.total_cost_usd:.4f} / ${snap.max_cost_usd:.2f}",
        ),
        _gauge(
            "loop",
            loop_ratio,
            f"{snap.loop_peak} / {snap.max_loop_repeats} repeats",
        ),
        _gauge("time", time_ratio, f"{uptime:.0f}s / {snap.max_runtime_seconds}s"),
        Text(""),
        Text(
            f"tools {snap.tool_calls}   ·   "
            f"tokens {tokens:,} "
            f"({snap.total_input_tokens:,} in / {snap.total_output_tokens:,} out)"
            + (f"   ·   steered {snap.steer_count}x" if snap.steer_count else ""),
            style="dim",
        ),
    )
    return Panel(
        body,
        title=f"[bold green]● ALIVE[/bold green]  {snap.name}",
        subtitle=f"[dim]policy {snap.policy_name} · {_mode(snap)}[/dim]",
        border_style="green",
    )


def _render_flatline(snap: VitalsSnapshot) -> RenderableType:
    items: list[RenderableType] = [
        Text("─" * _PULSE_WIDTH, style="bold red"),
        Text(""),
        Text(f"reason  {snap.terminate_reason or 'terminated'}", style="bold red"),
    ]
    if snap.recent_symptoms:
        items.append(Text(""))
        items.append(Text("symptoms", style="dim"))
        for s in snap.recent_symptoms:
            sym = s.get("symptom", "?")
            sev = s.get("severity", "?")
            reason = s.get("reason", "")
            if sev == "terminal":
                style = "red"
            elif sev == "steer":
                style = "cyan"
            else:
                style = "yellow"
            items.append(Text(f"  • {sym} ({sev})  {reason}", style=style))
    if snap.certificate_text:
        items.append(Text(""))
        items.append(Text(snap.certificate_text, style="dim"))
    return Panel(
        Group(*items),
        title=f"[bold red]† FLATLINE[/bold red]  {snap.name}",
        subtitle=f"[dim]policy {snap.policy_name} · {_mode(snap)}[/dim]",
        border_style="red",
    )


def _render_ended(snap: VitalsSnapshot) -> RenderableType:
    tokens = snap.total_input_tokens + snap.total_output_tokens
    body = Group(
        Text("session ended cleanly — no apoptosis.", style="green"),
        Text(""),
        Text(
            f"uptime {snap.uptime_seconds:.0f}s   ·   tools {snap.tool_calls}   ·   "
            f"${snap.total_cost_usd:.4f}   ·   tokens {tokens:,}",
            style="dim",
        ),
    )
    return Panel(
        body,
        title=f"[bold]■ ENDED[/bold]  {snap.name}",
        subtitle=f"[dim]policy {snap.policy_name}[/dim]",
        border_style="blue",
    )


def _render_no_signal(snap: VitalsSnapshot, age: float) -> RenderableType:
    body = Group(
        Text("─" * _PULSE_WIDTH, style="dim"),
        Text(""),
        Text(f"no signal — last update {age:.0f}s ago.", style="bold yellow"),
        Text(
            "the agent process may have crashed or been killed without a clean "
            "shutdown.",
            style="dim",
        ),
    )
    return Panel(
        body,
        title=f"[bold yellow]… NO SIGNAL[/bold yellow]  {snap.name}",
        subtitle=f"[dim]policy {snap.policy_name}[/dim]",
        border_style="yellow",
    )


# --- calibrate (Phase 4) -------------------------------------------------


@app.command()
def calibrate(
    policy: str = typer.Argument(
        ..., help="Policy name (strict, coding-default, permissive)."
    ),
) -> None:
    """Suggest threshold tweaks for a policy from operator feedback labels.

    Reads the one-click feedback verdicts collected on past death
    certificates and reports, per symptom, how often kills were labeled
    false-positive — and, where a numeric threshold maps cleanly, a looser
    value to *consider*. Advisory only: it never edits the policy, and it
    only ever loosens (executed-kill feedback can't see kills that should
    have fired but didn't, so it never recommends tightening).
    """
    _run(_calibrate(policy))


async def _calibrate(policy: str) -> None:
    async with HermeskillClient.from_config() as client:
        try:
            report = await client.get_calibration(policy)
        except NotFoundError:
            err_console.print(f"[red]unknown policy:[/red] {policy}")
            raise typer.Exit(4) from None

    console.print(
        f"[bold]calibration[/bold] [dim]·[/dim] policy [magenta]{report.policy_name}"
        f"[/magenta] [dim]·[/dim] {report.total_labeled_kills} labeled kill(s)"
    )

    if not report.symptoms:
        console.print(
            "[dim]no labeled kills yet — collect feedback via the link in "
            "death certificates, then re-run.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Symptom", style="cyan", no_wrap=True)
    table.add_column("n", justify="right")
    table.add_column("FP rate", justify="right")
    table.add_column("good/fp/miss/other", justify="center")
    table.add_column("Suggestion")
    table.add_column("Confidence", style="yellow")

    for s in report.symptoms:
        if s.suggested_value is not None and s.threshold_field is not None:
            suggestion = (
                f"[green]{s.threshold_field} "
                f"{s.current_value:g}→{s.suggested_value:g}[/green]"
            )
        else:
            suggestion = "[dim]—[/dim]"
        fp_rate = f"{s.false_positive_rate * 100:.0f}%"
        breakdown = (
            f"{s.good_kills}/{s.false_positives}/{s.missed_kills}/{s.other}"
        )
        table.add_row(
            s.symptom.value,
            str(s.total_labeled),
            fp_rate,
            breakdown,
            suggestion,
            s.confidence,
        )

    console.print(table)
    for s in report.symptoms:
        console.print(f"  [dim]• {s.symptom.value}:[/dim] {s.rationale}")
    console.print(f"\n[dim]{report.notes}[/dim]")


# --- placeholders (later milestones) -------------------------------------


@app.command()
def kill(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `hermeskill fleet`."),
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
    async with HermeskillClient.from_config() as client:
        # Step 0: fetch the agent + its policy so we can print the
        # worst-case latency budget. Two extra round trips before kill,
        # but the UX win is worth it — the user types `hermeskill kill` and
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
    from hermeskill.watcher import DEFAULT_KILL_POLL_INTERVAL

    return float(
        DEFAULT_KILL_POLL_INTERVAL
        + t.cooperative_grace_seconds
        + t.verification_timeout_seconds
    )


async def _watch_kill(
    client: HermeskillClient,
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
                f"`hermeskill logs {agent_id}` or `hermeskill fleet`. "
                f"kill_event={kill_event_id}"
            )
            raise typer.Exit(6)

        await asyncio.sleep(poll_interval)


@app.command()
def grant(
    agent_id: str = typer.Argument(..., help="Agent UUID. See `hermeskill fleet`."),
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

    async with HermeskillClient.from_config() as client:
        try:
            grant = await client.create_grant(
                agent_id,
                symptoms=parsed_symptoms,
                duration_seconds=duration_seconds,
                reason=reason,
            )
        except HermeskillError as exc:
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
    async with HermeskillClient.from_config() as client:
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
__all__ = [
    "_fleet",
    "_logs",
    "_print_event",
    "_render_fleet",
    "_render_vitals",
    "app",
    "monitor",
]
