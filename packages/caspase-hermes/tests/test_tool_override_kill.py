"""Kill path smoke tests: directive → tool_override → kill stub fires.

Tests the CaspasePlugin kill path:
  1. Kill directive arrives (state.request_termination called)
  2. Next pre_tool_call detects terminate_requested
  3. plugin._arm_kill_override calls ctx.tool_override(tool_name, stub)
  4. Stub is a _KillStub that raises SystemExit

Also covers:
  - Idempotency: arm_kill_override called twice for same tool only registers once
  - Kill stub raises SystemExit (BaseException), not a regular Exception
  - session_end flushes death cert (mocked client)
  - Manual kill via request_termination (simulating M4 poller)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from caspase.policies import resolve_policy
from caspase.watcher import WatcherState
from caspase_hermes.plugin import CaspasePlugin, _KillStub

from tests.conftest import make_state

# --- _KillStub ---------------------------------------------------------------


def test_kill_stub_raises_system_exit() -> None:
    stub = _KillStub("read_file", "loop detected")
    with pytest.raises(SystemExit):
        stub()


def test_kill_stub_raises_base_exception_not_exception() -> None:
    stub = _KillStub("read_file", "loop detected")
    raised = None
    try:
        stub()
    except Exception:
        raised = "Exception"
    except SystemExit:
        raised = "SystemExit"
    assert raised == "SystemExit", "kill stub must raise SystemExit (BaseException), not Exception"


# --- CaspasePlugin kill path --------------------------------------------------


def _make_plugin(ctx: MagicMock) -> tuple[CaspasePlugin, WatcherState]:
    """Build a CaspasePlugin with a pre-built state (bypasses async setup)."""
    client = MagicMock()
    plugin = CaspasePlugin(ctx, name="test", policy="coding-default", client=client)
    state = make_state()
    plugin._state = state
    return plugin, state


def test_pre_tool_call_arms_override_when_kill_pending(ctx: MagicMock) -> None:
    plugin, state = _make_plugin(ctx)
    state.request_termination("loop: repeated 5x")
    plugin.pre_tool_call("read_file", {"path": "/tmp/x"})
    ctx.tool_override.assert_called_once()
    tool_name_arg = ctx.tool_override.call_args[0][0]
    assert tool_name_arg == "read_file"


def test_pre_tool_call_arms_override_after_terminal_verdict(ctx: MagicMock) -> None:
    policy = resolve_policy("coding-default").model_copy(
        update={"thresholds": resolve_policy("coding-default").thresholds.model_copy(
            update={"max_loop_repeats": 3, "loop_window_actions": 10}
        )}
    )
    state = WatcherState(agent_id=uuid4(), name="test", policy=policy)
    plugin = CaspasePlugin(ctx, name="test", policy="coding-default", client=MagicMock())
    plugin._state = state

    inputs = {"path": "/tmp/loop"}
    plugin.pre_tool_call("read_file", inputs)
    plugin.pre_tool_call("read_file", inputs)
    plugin.pre_tool_call("read_file", inputs)  # 3rd — loop fires

    assert state.terminate_requested
    ctx.tool_override.assert_called()


def test_arm_kill_override_is_idempotent(ctx: MagicMock) -> None:
    plugin, state = _make_plugin(ctx)
    state.request_termination("test kill")
    plugin._arm_kill_override("read_file")
    plugin._arm_kill_override("read_file")  # second call — must not double-register
    ctx.tool_override.assert_called_once()


def test_arm_kill_override_covers_different_tools(ctx: MagicMock) -> None:
    plugin, state = _make_plugin(ctx)
    state.request_termination("test kill")
    plugin._arm_kill_override("read_file")
    plugin._arm_kill_override("write_file")  # different tool — should register
    assert ctx.tool_override.call_count == 2


def test_arm_kill_override_logs_on_ctx_failure(ctx: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    ctx.tool_override.side_effect = RuntimeError("ctx not ready")
    plugin, state = _make_plugin(ctx)
    state.request_termination("test kill")
    import logging
    with caplog.at_level(logging.WARNING, logger="caspase_hermes.plugin"):
        plugin._arm_kill_override("read_file")
    # Should not raise; should log
    assert "failed to arm kill override" in caplog.text.lower()


# --- session_end death cert --------------------------------------------------


def test_session_end_no_kill_does_not_post_cert(ctx: MagicMock) -> None:
    plugin, _state = _make_plugin(ctx)
    # No kill — session_end should not try to post
    with patch.object(plugin, "_post_death_cert_best_effort", new_callable=AsyncMock) as mock_post:
        plugin.session_end()
        mock_post.assert_not_called()


def test_session_end_posts_cert_when_kill_pending(ctx: MagicMock) -> None:
    plugin, state = _make_plugin(ctx)
    state.request_termination("loop: repeated 5x")

    posted = []

    async def _fake_post():
        posted.append(True)

    with patch.object(plugin, "_post_death_cert_best_effort", side_effect=_fake_post):
        import asyncio
        loop = asyncio.new_event_loop()
        # Patch asyncio.get_event_loop to return our loop for the session_end call
        with patch("asyncio.get_event_loop", return_value=loop):
            plugin.session_end()
        loop.close()

    assert posted, "death cert should have been posted"


# --- _KillStub as tool_override replacement ----------------------------------


def test_kill_stub_callable_signature_is_generic() -> None:
    """Kill stub must accept any args/kwargs (Hermes passes tool-specific args)."""
    stub = _KillStub("bash", "wall_clock exceeded")
    with pytest.raises(SystemExit):
        stub("some_arg", key="value")
