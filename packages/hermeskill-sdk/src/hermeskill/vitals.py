"""Live vitals snapshot — the cross-process channel that powers `hermeskill monitor`.

The death certificate is the *post-mortem*: written once, at the end. This module
is the opposite tense — the agent's vitals *while it runs*, refreshed on every hook
boundary so a separate process can watch them tick.

Why a file. The agent runs as one process (`hermes chat`); its ``WatcherState``
lives in that process's in-memory registry. ``hermeskill monitor`` runs as a
*different* process, and in the zero-config keyless path there is no control plane
to relay through. A file under ``~/.hermeskill/live/`` is the only channel that works
with no server. The plugin writes a snapshot each tick (best-effort, fail-open); the
monitor tails it. It's the keyless sibling of ``hermeskill logs --follow`` (which
tails the control plane), not a replacement.

Symmetry with ``certificate.py``: that module owns the post-mortem render + archival;
this one owns the live snapshot schema + atomic read/write. Neither depends on Hermes.

Writes are atomic via ``os.replace`` (works on Windows, where a plain rename onto an
existing target fails) so the monitor only ever sees a whole snapshot, never a
half-written one. Reads are tolerant: a torn or garbage file yields ``None`` rather
than crashing the display.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from hermeskill.watcher import WatcherState

LIVE_DIR = Path.home() / ".hermeskill" / "live"

# How many trailing symptoms to carry so the flatline panel is self-sufficient.
_RECENT_SYMPTOMS = 8

# Default staleness cutoff for `iter_live_snapshots` — a `running` snapshot older
# than this means the agent process likely died without firing `session_end`
# (crash / kill -9). The monitor shows "no signal" past this; it's only a
# backstop, the happy path is the `terminated`/`ended_clean` status written by
# `session_end`.
DEFAULT_MAX_AGE_SECONDS = 30.0

# How old a live file must be before a new session sweeps it. Generous on
# purpose: this only runs at registration, and an *active* agent in another
# process refreshes its file on every hook — so a 1h cutoff clears genuinely
# dead-session files without racing a long-idle but live agent.
DEFAULT_SWEEP_AGE_SECONDS = 3600.0

Status = Literal["running", "terminated", "ended_clean"]


class VitalsSnapshot(BaseModel):
    """A point-in-time view of one agent's supervision state, written each tick.

    Everything the monitor needs to render — including the full flatline view —
    is carried here, so the monitor reads exactly one file and never has to glob
    for the death cert or race the cert write.
    """

    model_config = ConfigDict(extra="forbid")

    # identity
    agent_id: UUID
    name: str
    policy_name: str
    offline: bool = False

    # status — the only reliable "dead vs. just finished" discriminator, since
    # `session_end` fires on both clean exits and kills.
    status: Status = "running"

    # timing. `uptime_seconds` is the authoritative value at write time;
    # `written_at` lets the monitor extrapolate `uptime + (now - written_at)`
    # so the clock ticks smoothly between agent actions and the wall-clock
    # gauge lines up with `check_wall_clock`.
    uptime_seconds: float = 0.0
    written_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # counters
    tool_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    # gauge inputs (raw — the monitor computes ratios). `loop_peak` is the exact
    # value `check_loop` triggers on (see checks.loop_peak), so the gauge can
    # never disagree with the kill.
    loop_peak: int = 0
    loop_window: int = 0
    max_loop_repeats: int = 1
    max_cost_usd: float = 0.0
    max_tokens_per_run: int = 1
    max_runtime_seconds: int = 1

    # death view — always present so the flatline panel needs nothing else.
    terminate_reason: str | None = None
    recent_symptoms: list[dict[str, Any]] = Field(default_factory=list)
    # The rendered death cert, spliced in by `session_end` when local_cert is
    # on. A bonus, never a dependency — the flatline still renders without it.
    certificate_text: str | None = None


def snapshot_from_state(
    state: WatcherState,
    *,
    status: Status = "running",
    certificate_text: str | None = None,
) -> VitalsSnapshot:
    """Build a :class:`VitalsSnapshot` from a live ``WatcherState``.

    Pure read over state — no I/O, no mutation. ``loop_peak`` is computed via
    :func:`hermeskill.checks.loop_peak`, the same helper ``check_loop`` uses.
    """
    from hermeskill.checks import loop_peak

    _, peak = loop_peak(state)
    t = state.policy.thresholds
    return VitalsSnapshot(
        agent_id=state.agent_id,
        name=state.name,
        policy_name=state.policy.name,
        offline=state.offline,
        status=status,
        uptime_seconds=state.uptime_seconds(),
        written_at=datetime.now(UTC),
        tool_calls=state.tool_call_count,
        total_input_tokens=state.total_input_tokens,
        total_output_tokens=state.total_output_tokens,
        total_cost_usd=state.total_cost_usd,
        loop_peak=peak,
        loop_window=len(state.loop_signatures),
        max_loop_repeats=t.max_loop_repeats,
        max_cost_usd=t.max_cost_usd,
        max_tokens_per_run=t.max_tokens_per_run,
        max_runtime_seconds=t.max_runtime_seconds,
        terminate_reason=state.terminate_reason,
        recent_symptoms=list(state.symptoms_log[-_RECENT_SYMPTOMS:]),
        certificate_text=certificate_text,
    )


def snapshot_path(agent_id: UUID, *, directory: Path | None = None) -> Path:
    return (directory or LIVE_DIR) / f"{agent_id}.json"


def write_snapshot(
    snapshot: VitalsSnapshot,
    *,
    directory: Path | None = None,
) -> Path:
    """Atomically write ``snapshot`` to ``<dir>/<agent_id>.json``.

    Temp file in the same directory + ``os.replace`` so a reader sees either the
    old snapshot or the new one, never a partial write. ``os.replace`` overwrites
    the target on every OS (unlike ``os.rename`` on Windows).
    """
    target_dir = directory or LIVE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{snapshot.agent_id}.json"
    payload = json.dumps(snapshot.model_dump(mode="json"), indent=2)
    # Unique temp name (pid-tagged) so concurrent writers in the same process
    # don't clobber each other's temp file before the replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=target_dir, prefix=f".{snapshot.agent_id}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        Path(tmp_name).replace(path)
    except BaseException:
        # Don't leave a temp file behind on failure.
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
    return path


def read_snapshot(path: Path) -> VitalsSnapshot | None:
    """Load a snapshot. Returns ``None`` on a missing, torn, or invalid file.

    The monitor polls this on a tight loop; a mid-write read (should be
    impossible given ``os.replace``, but disks lie) or a hand-corrupted file
    must degrade to "no reading", never crash the display.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        return VitalsSnapshot.model_validate_json(raw)
    except Exception:
        return None


def delete_snapshot(agent_id: UUID, *, directory: Path | None = None) -> None:
    """Best-effort removal of an agent's live file (e.g. on next session start)."""
    with contextlib.suppress(OSError):
        snapshot_path(agent_id, directory=directory).unlink()


def sweep_live_dir(
    *,
    directory: Path | None = None,
    max_age_seconds: float = DEFAULT_SWEEP_AGE_SECONDS,
) -> None:
    """Best-effort removal of live files older than ``max_age_seconds``.

    Called at session registration to clear dead-session files (incl. stale
    terminal ones the monitor keeps forever) so they don't accumulate. Every
    failure is swallowed — sweeping is hygiene, never a hard requirement.
    """
    target_dir = directory or LIVE_DIR
    if not target_dir.exists():
        return
    cutoff = time.time() - max_age_seconds
    for p in target_dir.glob("*.json"):
        with contextlib.suppress(OSError):
            if p.stat().st_mtime < cutoff:
                p.unlink()


def iter_live_snapshots(
    *,
    directory: Path | None = None,
    max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS,
) -> list[VitalsSnapshot]:
    """Return readable live snapshots, freshest first.

    A ``running`` snapshot whose file is older than ``max_age_seconds`` is
    dropped (its agent likely died without ``session_end`` — crash / kill -9).
    Terminal snapshots (``terminated`` / ``ended_clean``) are kept regardless of
    age so a just-finished run is still visible. ``max_age_seconds=None``
    disables the filter entirely.
    """
    target_dir = directory or LIVE_DIR
    if not target_dir.exists():
        return []
    now = time.time()
    out: list[VitalsSnapshot] = []
    for p in target_dir.glob("*.json"):
        snap = read_snapshot(p)
        if snap is None:
            continue
        if (
            max_age_seconds is not None
            and snap.status == "running"
            and (now - p.stat().st_mtime) > max_age_seconds
        ):
            continue
        out.append(snap)
    out.sort(key=lambda s: s.written_at, reverse=True)
    return out
