"""The Hermes plugin writes a live-vitals snapshot each hook tick.

These pin the producer side of `hermeskill monitor`:
- a snapshot lands (status=running) after a tool boundary
- session_end writes the terminal snapshot, with the right status
  (terminated vs ended_clean) and the death-cert text spliced in on a kill
- the writes are fail-open: a broken snapshot write must never escape a hook

The live + cert directories are redirected to a tmp path by the autouse
`_isolate_hermeskill_dirs` fixture in conftest, so nothing touches real home.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from hermeskill import vitals
from hermeskill.vitals import read_snapshot, snapshot_path
from hermeskill.watcher import WatcherState
from hermeskill_hermes import plugin as plugin_mod
from hermeskill_hermes.plugin import HermeskillPlugin

from tests.conftest import make_policy, make_state


def _make_plugin(
    state: WatcherState | None = None,
    *,
    local_cert: bool = True,
    live_vitals: bool = True,
) -> tuple[HermeskillPlugin, WatcherState]:
    plugin = HermeskillPlugin(
        name="test",
        policy="coding-default",
        client=MagicMock(),
        forced_offline=True,  # keep session_end off the control-plane path
        local_cert=local_cert,
        live_vitals=live_vitals,
    )
    st = state or make_state()
    st.offline = True
    plugin._state = st
    return plugin, st


def _read(state: WatcherState) -> vitals.VitalsSnapshot | None:
    return read_snapshot(snapshot_path(state.agent_id))


# --- running snapshots -------------------------------------------------------


def test_pre_tool_call_writes_running_snapshot() -> None:
    plugin, state = _make_plugin()
    plugin.pre_tool_call("read_file", {"path": "/tmp/x"})

    snap = _read(state)
    assert snap is not None
    assert snap.status == "running"
    assert snap.tool_calls == 1
    assert snap.agent_id == state.agent_id


def test_post_api_request_snapshot_tracks_cost() -> None:
    plugin, state = _make_plugin()
    plugin.post_api_request("claude-opus-4-7", {"input_tokens": 100, "output_tokens": 50}, 0.1)

    snap = _read(state)
    assert snap is not None
    assert snap.total_input_tokens == 100
    assert snap.total_output_tokens == 50


# --- terminal snapshots ------------------------------------------------------


def test_session_end_writes_terminated_snapshot_with_cert() -> None:
    policy = make_policy(max_cost_usd=0.00001)
    state = WatcherState(agent_id=uuid4(), name="t", policy=policy)
    state.offline = True
    state.total_cost_usd = 1.0  # over cap → arms kill on next pre_tool_call
    plugin, _ = _make_plugin(state, local_cert=True)

    plugin.pre_tool_call("read_file", {})  # fires token_runaway Terminal
    assert state.terminate_requested
    plugin.session_end()

    snap = _read(state)
    assert snap is not None
    assert snap.status == "terminated"
    assert snap.terminate_reason is not None
    # local_cert is on → the rendered cert text is spliced into the snapshot.
    assert snap.certificate_text is not None
    assert "DEATH CERTIFICATE" in snap.certificate_text


def test_session_end_clean_writes_ended_snapshot() -> None:
    plugin, state = _make_plugin(local_cert=True)
    plugin.pre_tool_call("read_file", {"path": "/tmp/x"})  # healthy
    assert not state.terminate_requested
    plugin.session_end()

    snap = _read(state)
    assert snap is not None
    assert snap.status == "ended_clean"
    assert snap.certificate_text is None  # no kill → no cert


def test_terminated_snapshot_without_local_cert_still_flatlines() -> None:
    """With local_cert off there's no cert text, but the flatline view is still
    self-sufficient: status + reason must travel."""
    policy = make_policy(max_cost_usd=0.00001)
    state = WatcherState(agent_id=uuid4(), name="t", policy=policy)
    state.offline = True
    state.total_cost_usd = 1.0
    plugin, _ = _make_plugin(state, local_cert=False)

    plugin.pre_tool_call("read_file", {})
    plugin.session_end()

    snap = _read(state)
    assert snap is not None
    assert snap.status == "terminated"
    assert snap.terminate_reason is not None
    assert snap.certificate_text is None


# --- fail-open ---------------------------------------------------------------


def test_write_vitals_failopen_when_write_raises(monkeypatch) -> None:
    """A broken snapshot write must never escape a Hermes hook — same contract
    as the bridge check guards and the local-cert emit."""

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("disk full")

    monkeypatch.setattr(plugin_mod, "write_snapshot", _boom)
    plugin, state = _make_plugin()
    # Must not raise despite the write blowing up.
    plugin.pre_tool_call("read_file", {"path": "/tmp/x"})
    assert not state.terminate_requested


def test_live_vitals_disabled_writes_nothing() -> None:
    """HERMESKILL_LIVE=0 (live_vitals=False) → no per-tick file write at all."""
    plugin, state = _make_plugin(live_vitals=False)
    plugin.pre_tool_call("read_file", {"path": "/tmp/x"})
    plugin.post_api_request("claude-opus-4-7", {"input_tokens": 10, "output_tokens": 5}, 0.1)
    assert _read(state) is None
