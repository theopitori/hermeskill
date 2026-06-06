"""Kill path tests: pre_tool_call returns Hermes' block directive when armed.

Tests the HermeskillPlugin kill path against Hermes v0.14's documented hook
contract:
  1. Kill directive arrives (state.request_termination called by a check
     or the manual-kill poller)
  2. Next pre_tool_call returns {"action": "block", "message": "hermeskill: ..."}
  3. Hermes wraps that into a tool error and refuses to run the tool
  4. on_session_end fires when the agent's loop ends naturally → death cert

Also covers:
  - Block directive shape (action + message keys)
  - Block message contains the kill reason
  - Healthy state returns None (tool proceeds normally)
  - Loop detection arms kill and returns block on the 3rd repeat
  - Manual kill via request_termination (simulating the manual-kill poller)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from hermeskill.policies import resolve_policy
from hermeskill.watcher import WatcherState
from hermeskill_hermes.plugin import HermeskillPlugin

from tests.conftest import make_state


def _make_plugin() -> tuple[HermeskillPlugin, WatcherState]:
    """Build a HermeskillPlugin with a pre-built state (bypasses async setup)."""
    client = MagicMock()
    plugin = HermeskillPlugin(name="test", policy="coding-default", client=client)
    state = make_state()
    plugin._state = state
    return plugin, state


# --- block directive shape ---------------------------------------------------


def test_pre_tool_call_returns_block_when_kill_already_pending() -> None:
    plugin, state = _make_plugin()
    state.request_termination("loop: repeated 5x")
    directive = plugin.pre_tool_call("read_file", {"path": "/tmp/x"})
    assert directive is not None
    assert directive["action"] == "block"
    assert "loop: repeated 5x" in directive["message"]


def test_pre_tool_call_returns_none_when_healthy() -> None:
    plugin, _state = _make_plugin()
    directive = plugin.pre_tool_call("read_file", {"path": "/tmp/x"})
    assert directive is None


def test_block_directive_shape_is_string_keyed_dict() -> None:
    """Hermes' get_pre_tool_call_block_message only honours dict results with
    action='block' and a non-empty string message."""
    plugin, state = _make_plugin()
    state.request_termination("manual_kill")
    directive = plugin.pre_tool_call("any_tool", {})
    assert isinstance(directive, dict)
    assert set(directive.keys()) == {"action", "message"}
    assert isinstance(directive["action"], str)
    assert isinstance(directive["message"], str)
    assert directive["message"]  # non-empty


def test_block_message_names_hermeskill_and_reason() -> None:
    plugin, state = _make_plugin()
    state.request_termination("token_runaway: $25.41 exceeded $25.00")
    directive = plugin.pre_tool_call("read_file", {})
    assert directive is not None
    msg = directive["message"]
    assert "hermeskill" in msg.lower()
    assert "token_runaway" in msg


# --- triggers that arm the kill mid-call -------------------------------------


def test_pre_tool_call_returns_block_after_loop_terminal_fires() -> None:
    """3rd identical call fires loop check → kill is armed → block returned."""
    policy = resolve_policy("coding-default").model_copy(
        update={"thresholds": resolve_policy("coding-default").thresholds.model_copy(
            update={"max_loop_repeats": 3, "loop_window_actions": 10}
        )}
    )
    state = WatcherState(agent_id=uuid4(), name="test", policy=policy)
    plugin = HermeskillPlugin(name="test", policy="coding-default", client=MagicMock())
    plugin._state = state

    args = {"path": "/tmp/loop"}
    assert plugin.pre_tool_call("read_file", args) is None
    assert plugin.pre_tool_call("read_file", args) is None
    directive = plugin.pre_tool_call("read_file", args)  # 3rd — loop fires

    assert state.terminate_requested
    assert directive is not None
    assert directive["action"] == "block"


def test_pre_tool_call_returns_block_on_scope_violation() -> None:
    """Scope violation arms kill on the first disallowed call."""
    policy = resolve_policy("coding-default").model_copy(
        update={"tool_allowlist": ["read_file"]}
    )
    state = WatcherState(agent_id=uuid4(), name="test", policy=policy)
    plugin = HermeskillPlugin(name="test", policy="coding-default", client=MagicMock())
    plugin._state = state

    directive = plugin.pre_tool_call("delete_everything", {})
    assert directive is not None
    assert directive["action"] == "block"
    assert state.terminate_requested


def test_pre_tool_call_keeps_blocking_after_first_kill() -> None:
    """After kill, every subsequent pre_tool_call must still return block.
    This is what stops the agent from continuing to call other tools."""
    plugin, state = _make_plugin()
    state.request_termination("loop")
    for tool in ("read_file", "write_file", "terminal", "browser_navigate"):
        directive = plugin.pre_tool_call(tool, {"some": "args"})
        assert directive is not None
        assert directive["action"] == "block", f"tool {tool} should block"


# --- loop steer (soft intervention) ------------------------------------------


def _steer_plugin() -> tuple[HermeskillPlugin, WatcherState]:
    """Plugin whose policy steers at 3 and kills at 5 (window 10)."""
    base = resolve_policy("coding-default")
    thresholds = base.thresholds.model_copy(
        update={
            "max_loop_repeats": 5,
            "loop_steer_repeats": 3,
            "loop_window_actions": 10,
        }
    )
    policy = base.model_copy(update={"thresholds": thresholds})
    state = WatcherState(agent_id=uuid4(), name="test", policy=policy)
    plugin = HermeskillPlugin(name="test", policy="coding-default", client=MagicMock())
    plugin._state = state
    return plugin, state


def test_pre_tool_call_steers_in_band_without_terminating() -> None:
    """3rd identical call (steer threshold) returns a block directive, but the
    agent is NOT terminated — the session continues."""
    plugin, state = _steer_plugin()
    args = {"path": "/tmp/loop"}
    assert plugin.pre_tool_call("read_file", args) is None  # 1
    assert plugin.pre_tool_call("read_file", args) is None  # 2
    directive = plugin.pre_tool_call("read_file", args)  # 3 → steer

    assert directive is not None
    assert directive["action"] == "block"
    assert "steer" in directive["message"].lower()
    assert not state.terminate_requested
    assert state.steer_count == 1


def test_steer_directive_does_not_tell_agent_to_end_session() -> None:
    """A steer should nudge a course-change, not a shutdown — unlike the kill
    directive it must not instruct the agent to end the session."""
    plugin, _state = _steer_plugin()
    args = {"path": "/tmp/loop"}
    for _ in range(2):
        plugin.pre_tool_call("read_file", args)
    directive = plugin.pre_tool_call("read_file", args)
    assert directive is not None
    msg = directive["message"].lower()
    assert "change approach" in msg
    assert "end the session" not in msg


def test_steer_escalates_to_kill_at_cap() -> None:
    """Persisting through the steer band lands on apoptosis at the kill cap."""
    plugin, state = _steer_plugin()
    args = {"path": "/tmp/loop"}
    directives = [plugin.pre_tool_call("read_file", args) for _ in range(5)]
    # 1,2 → None; 3,4 → steer (alive); 5 → kill.
    assert directives[0] is None
    assert directives[1] is None
    assert directives[2] is not None and not _is_kill(directives[2])
    assert directives[3] is not None and not _is_kill(directives[3])
    assert directives[4] is not None and _is_kill(directives[4])
    assert state.terminate_requested
    assert state.steer_count == 2  # steered twice before the kill


def test_steer_then_changed_approach_proceeds() -> None:
    """After a steer, a different call proceeds (None) and the agent stays
    alive — the recoverable path the whole feature exists for."""
    plugin, state = _steer_plugin()
    loop_args = {"path": "/tmp/loop"}
    for _ in range(3):
        plugin.pre_tool_call("read_file", loop_args)  # steers on the 3rd
    assert state.steer_count == 1
    # Agent obeys and does something different.
    directive = plugin.pre_tool_call("write_file", {"path": "/tmp/new"})
    assert directive is None
    assert not state.terminate_requested


def test_granted_loop_neither_steers_nor_kills() -> None:
    """A live `loop` grant suppresses both the steer and the kill — a
    legitimately-looping granted agent proceeds untouched."""
    plugin, state = _steer_plugin()
    state.grants = [
        {"id": "g1", "symptoms": ["loop"], "reason": "intentional poll"}
    ]
    args = {"path": "/tmp/poll"}
    # Push well past both the steer (3) and kill (5) thresholds.
    for _ in range(8):
        assert plugin.pre_tool_call("read_file", args) is None
    assert not state.terminate_requested
    assert state.steer_count == 0


def _is_kill(directive: dict[str, str] | None) -> bool:
    """A kill directive says 'apoptosis'; a steer says 'loop-steer'."""
    return directive is not None and "apoptosis" in directive["message"].lower()


# --- session_end death cert --------------------------------------------------


def test_session_end_no_kill_does_not_post_cert() -> None:
    plugin, _state = _make_plugin()
    with patch.object(plugin, "_post_death_cert_best_effort", new_callable=AsyncMock) as mock_post:
        plugin.session_end()
        mock_post.assert_not_called()


def test_session_end_posts_cert_when_kill_pending() -> None:
    plugin, state = _make_plugin()
    state.request_termination("loop: repeated 5x")

    posted = []

    async def _fake_post() -> None:
        posted.append(True)

    with patch.object(plugin, "_post_death_cert_best_effort", side_effect=_fake_post):
        plugin.session_end()

    assert posted, "death cert should have been posted"


# --- post_tool_call does NOT itself block ------------------------------------


def test_post_tool_call_arms_kill_but_returns_nothing() -> None:
    """post_tool_call runs checks and may flip terminate_requested, but the
    block happens at the next pre_tool_call. The post hook itself has no
    return-value channel into Hermes."""
    policy = resolve_policy("coding-default").model_copy(
        update={"thresholds": resolve_policy("coding-default").thresholds.model_copy(
            update={"max_cost_usd": 0.00001}
        )}
    )
    state = WatcherState(agent_id=uuid4(), name="test", policy=policy)
    plugin = HermeskillPlugin(name="test", policy="coding-default", client=MagicMock())
    plugin._state = state
    state.total_cost_usd = 1.0  # already over cap

    # post_tool_call: returns None, but arms the kill
    result = plugin.post_tool_call("read_file", {}, "some_output")
    assert result is None
    assert state.terminate_requested

    # Next pre_tool_call: returns the block directive
    directive = plugin.pre_tool_call("read_file", {})
    assert directive is not None
    assert directive["action"] == "block"
