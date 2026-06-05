"""Tests for hermeskill.vitals — the live-vitals snapshot sink.

Coverage:
- write → read round-trips the full snapshot
- write is atomic (overwrites, leaves no temp files behind)
- snapshot_from_state's loop_peak equals what check_loop counts on
- read_snapshot is tolerant: missing / garbage file → None
- iter_live_snapshots drops stale `running` files but keeps terminal ones
- delete_snapshot removes the live file
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

from hermeskill.checks import loop_peak
from hermeskill.policies import resolve_policy
from hermeskill.types import Policy, PolicyThresholds, SymptomType
from hermeskill.vitals import (
    VitalsSnapshot,
    delete_snapshot,
    iter_live_snapshots,
    read_snapshot,
    snapshot_from_state,
    snapshot_path,
    sweep_live_dir,
    write_snapshot,
)
from hermeskill.watcher import WatcherState


def _state(policy: Policy | None = None) -> WatcherState:
    return WatcherState(
        agent_id=uuid4(),
        name="t",
        policy=policy or resolve_policy("coding-default"),
    )


def _policy_with(**overrides: object) -> Policy:
    base = resolve_policy("coding-default")
    t = base.thresholds.model_dump()
    t.update(overrides)
    return base.model_copy(update={"thresholds": PolicyThresholds(**t)})


# --- round trip --------------------------------------------------------------


def test_write_read_round_trip(tmp_path: Path) -> None:
    s = _state()
    s.record_tool_call("read_file", {"path": "a"})
    s.record_llm_call("claude-opus-4-7", 100, 50)
    snap = snapshot_from_state(s, status="running")
    path = write_snapshot(snap, directory=tmp_path)

    loaded = read_snapshot(path)
    assert loaded is not None
    assert loaded.agent_id == s.agent_id
    assert loaded.tool_calls == 1
    assert loaded.total_input_tokens == 100
    assert loaded.total_output_tokens == 50
    assert loaded.status == "running"
    assert loaded.policy_name == s.policy.name


def test_snapshot_path_uses_agent_id(tmp_path: Path) -> None:
    s = _state()
    path = write_snapshot(snapshot_from_state(s), directory=tmp_path)
    assert path == snapshot_path(s.agent_id, directory=tmp_path)
    assert path.name == f"{s.agent_id}.json"


# --- atomicity ---------------------------------------------------------------


def test_write_overwrites_and_leaves_no_temp_files(tmp_path: Path) -> None:
    s = _state()
    write_snapshot(snapshot_from_state(s, status="running"), directory=tmp_path)
    s.record_tool_call("read_file", {"path": "a"})
    write_snapshot(snapshot_from_state(s, status="running"), directory=tmp_path)

    # Exactly one JSON file (the agent's), and no leftover .tmp files.
    json_files = list(tmp_path.glob("*.json"))
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(json_files) == 1
    assert tmp_files == []

    loaded = read_snapshot(json_files[0])
    assert loaded is not None
    assert loaded.tool_calls == 1  # the second write won


# --- loop_peak parity --------------------------------------------------------


def test_snapshot_loop_peak_matches_check_loop(tmp_path: Path) -> None:
    """The gauge input must be the exact count check_loop triggers on, so the
    monitor can never show 4/5 at the instant the agent dies at 'loop'."""
    p = _policy_with(max_loop_repeats=5, loop_window_actions=20)
    s = _state(p)
    for _ in range(4):
        s.record_tool_call("read_file", {"path": "same"})
    s.record_tool_call("write_file", {"path": "other"})

    _, expected = loop_peak(s)
    snap = snapshot_from_state(s)
    assert snap.loop_peak == expected == 4
    assert snap.max_loop_repeats == 5
    assert snap.loop_window == 5  # 5 tool calls recorded


# --- tolerant read -----------------------------------------------------------


def test_read_snapshot_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_snapshot(tmp_path / "nope.json") is None


def test_read_snapshot_garbage_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    assert read_snapshot(bad) is None


def test_read_snapshot_wrong_shape_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "wrong.json"
    bad.write_text('{"unexpected": "fields only"}', encoding="utf-8")
    assert read_snapshot(bad) is None


# --- iter_live_snapshots -----------------------------------------------------


def test_iter_live_skips_stale_running(tmp_path: Path) -> None:
    s = _state()
    path = write_snapshot(snapshot_from_state(s, status="running"), directory=tmp_path)
    # Age the file well past the cutoff.
    old = time.time() - 9999
    os.utime(path, (old, old))

    assert iter_live_snapshots(directory=tmp_path, max_age_seconds=30.0) == []
    # max_age=None disables the filter — the snapshot comes back.
    assert len(iter_live_snapshots(directory=tmp_path, max_age_seconds=None)) == 1


def test_iter_live_keeps_stale_terminal(tmp_path: Path) -> None:
    """A terminated agent's snapshot is the last word — it must survive the
    staleness filter so a just-finished kill is still visible."""
    s = _state()
    s.request_termination("loop")
    path = write_snapshot(
        snapshot_from_state(s, status="terminated"), directory=tmp_path
    )
    old = time.time() - 9999
    os.utime(path, (old, old))

    live = iter_live_snapshots(directory=tmp_path, max_age_seconds=30.0)
    assert len(live) == 1
    assert live[0].status == "terminated"


def test_iter_live_sorts_freshest_first(tmp_path: Path) -> None:
    older = _state()
    newer = _state()
    write_snapshot(snapshot_from_state(older, status="running"), directory=tmp_path)
    time.sleep(0.01)
    write_snapshot(snapshot_from_state(newer, status="running"), directory=tmp_path)

    live = iter_live_snapshots(directory=tmp_path, max_age_seconds=None)
    assert live[0].agent_id == newer.agent_id


def test_iter_live_empty_dir(tmp_path: Path) -> None:
    assert iter_live_snapshots(directory=tmp_path / "missing") == []


# --- delete ------------------------------------------------------------------


def test_delete_snapshot(tmp_path: Path) -> None:
    s = _state()
    path = write_snapshot(snapshot_from_state(s), directory=tmp_path)
    assert path.exists()
    delete_snapshot(s.agent_id, directory=tmp_path)
    assert not path.exists()
    # Idempotent — deleting a missing file is a no-op, not an error.
    delete_snapshot(s.agent_id, directory=tmp_path)


# --- sweep -------------------------------------------------------------------


def test_sweep_removes_old_keeps_fresh(tmp_path: Path) -> None:
    old_state = _state()
    new_state = _state()
    p_old = write_snapshot(snapshot_from_state(old_state), directory=tmp_path)
    p_new = write_snapshot(snapshot_from_state(new_state), directory=tmp_path)
    aged = time.time() - 9999
    os.utime(p_old, (aged, aged))

    sweep_live_dir(directory=tmp_path, max_age_seconds=3600)
    assert not p_old.exists()
    assert p_new.exists()


def test_sweep_missing_dir_is_noop(tmp_path: Path) -> None:
    sweep_live_dir(directory=tmp_path / "missing")  # must not raise


# --- terminal view self-sufficiency ------------------------------------------


def test_terminated_snapshot_carries_death_view(tmp_path: Path) -> None:
    """The flatline panel must render from the snapshot alone — reason +
    symptoms travel even without the (optional) cert text."""
    p = _policy_with(max_cost_usd=0.00001)
    s = _state(p)
    s.total_cost_usd = 1.0
    s.record_symptom(symptom=SymptomType.LOOP, severity="terminal", reason="boom")
    s.request_termination("token_runaway: over cap")
    snap = snapshot_from_state(s, status="terminated")
    assert snap.terminate_reason == "token_runaway: over cap"
    assert snap.recent_symptoms
    assert snap.recent_symptoms[-1]["reason"] == "boom"
    assert isinstance(snap, VitalsSnapshot)
