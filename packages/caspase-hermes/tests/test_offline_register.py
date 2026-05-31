"""Fail-open tests: control plane unreachable at registration time.

A safety supervisor must never silently fail to load. If Hermes' plugin
loader caught an exception out of ``register()`` it would run the agent with
zero hooks and zero supervision. So when the control plane is unreachable at
registration, the plugin falls back to LOCAL-ONLY mode: it mints a local
agent_id, wires all five hooks, and keeps in-process symptom checks live.
Only operator visibility / manual kill / grants / death-cert archival degrade.

Covered here:
  - ``setup()`` swallows a transport failure and still builds the watcher,
    marking it offline with a locally-minted UUID
  - ``async_register()`` still wires all five Hermes hooks when offline
  - loop detection still fires in-process and returns the block directive
    even though the control plane never saw this agent
  - auth/server errors are NOT swallowed (those are misconfiguration)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import caspase_hermes
import pytest
from caspase.client import AuthError, TransportError
from caspase.watcher import (
    _reset_registry_for_tests,
    all_watchers,
    get_watcher,
)
from caspase_hermes.plugin import CaspasePlugin

VALID_HOOK_NAMES = {
    "pre_tool_call",
    "post_tool_call",
    "pre_llm_call",
    "post_api_request",
    "on_session_end",
}


@pytest.fixture(autouse=True)
def _clean_registry() -> object:
    _reset_registry_for_tests()
    caspase_hermes._current_plugin = None
    yield
    _reset_registry_for_tests()
    caspase_hermes._current_plugin = None


def _offline_client() -> MagicMock:
    """Mock CaspaseClient whose register_agent fails as if the CP were down."""
    client = MagicMock()
    client.register_agent = AsyncMock(
        side_effect=TransportError("POST /agents: ConnectError: connection refused")
    )
    return client


# --- setup() fail-open -------------------------------------------------------


async def test_setup_falls_back_to_local_when_control_plane_unreachable() -> None:
    plugin = CaspasePlugin(name="t", policy="coding-default", client=_offline_client())
    with patch("caspase_hermes.plugin.ensure_worker_started") as worker:
        await plugin.setup()

    state = plugin._state
    assert state is not None, "watcher must be built even when registration fails"
    assert state.offline is True
    assert isinstance(state.agent_id, UUID)
    assert state.watchdog is not None
    # Registered in the process registry so a later (online) session's worker
    # could still see it.
    assert get_watcher(state.agent_id) is state
    # Worker is NOT booted offline: it only talks to the control plane
    # (heartbeats, event drain, kill poll), which can never succeed here and
    # would log a connection-refused traceback every few seconds. Local symptom
    # checks + the L2 watchdog run independently, so the kill path is intact.
    worker.assert_not_called()


async def test_setup_does_not_swallow_auth_errors() -> None:
    """A bad API key is a misconfiguration, not a transient outage — surface it."""
    client = MagicMock()
    client.register_agent = AsyncMock(side_effect=AuthError("401: bad key"))
    plugin = CaspasePlugin(name="t", policy="coding-default", client=client)
    with (
        patch("caspase_hermes.plugin.ensure_worker_started"),
        pytest.raises(AuthError),
    ):
        await plugin.setup()
    assert plugin._state is None


# --- async_register still wires all five hooks when offline ------------------


async def test_offline_async_register_wires_all_five_hooks() -> None:
    ctx = MagicMock()
    ctx.register_hook = MagicMock()

    with (
        patch("caspase_hermes.SDKConfig") as sdk_config,
        patch("caspase_hermes.CaspaseClient") as client_cls,
        patch("caspase_hermes.plugin.ensure_worker_started"),
    ):
        # Unset policy/agent_name → adapter applies its own defaults.
        loaded_config = MagicMock()
        loaded_config.policy = None
        loaded_config.agent_name = None
        sdk_config.load.return_value = loaded_config
        client_cls.from_config.return_value = _offline_client()
        await caspase_hermes.async_register(ctx)

    registered = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert registered == VALID_HOOK_NAMES
    assert caspase_hermes._current_plugin is not None
    assert caspase_hermes._current_plugin._state is not None
    assert caspase_hermes._current_plugin._state.offline is True


# --- local symptom checks still fire while offline ---------------------------


async def test_offline_loop_detection_still_fires_and_blocks() -> None:
    """coding-default fires loop at the 5th identical signature. The control
    plane never saw this agent, yet the in-process check still arms the kill
    and pre_tool_call returns Hermes' block directive."""
    plugin = CaspasePlugin(name="t", policy="coding-default", client=_offline_client())
    with patch("caspase_hermes.plugin.ensure_worker_started"):
        await plugin.setup()

    assert plugin._state is not None and plugin._state.offline is True

    args = {"path": "/tmp/loop"}
    for _ in range(4):
        assert plugin.pre_tool_call("read_file", args) is None
    directive = plugin.pre_tool_call("read_file", args)  # 5th — loop fires

    assert plugin._state.terminate_requested
    assert directive is not None
    assert directive["action"] == "block"
    assert "caspase" in directive["message"].lower()
    # The agent is still locally tracked even though it's invisible to the CP.
    assert plugin._state in all_watchers()


# --- keyless (no API key) → forced offline, zero network ---------------------


async def test_forced_offline_skips_registration_and_worker() -> None:
    """No API key → forced_offline: registration is never attempted (so we
    never hit the not-swallowed AuthError against a reachable localhost) and
    the network worker/poller are never booted."""
    client = MagicMock()
    client.register_agent = AsyncMock(
        side_effect=AssertionError("register_agent must not be called when forced_offline")
    )
    plugin = CaspasePlugin(
        name="t", policy="coding-default", client=client, forced_offline=True
    )
    with patch("caspase_hermes.plugin.ensure_worker_started") as worker:
        await plugin.setup()

    assert plugin._state is not None
    assert plugin._state.offline is True
    client.register_agent.assert_not_called()
    worker.assert_not_called()


async def test_keyless_async_register_is_forced_offline() -> None:
    """register() with no API key builds the client with allow_keyless=True and
    runs the plugin forced-offline — all five hooks wired, no worker booted."""
    ctx = MagicMock()
    ctx.register_hook = MagicMock()

    with (
        patch("caspase_hermes.SDKConfig") as sdk_config,
        patch("caspase_hermes.CaspaseClient") as client_cls,
        patch("caspase_hermes.plugin.ensure_worker_started") as worker,
    ):
        loaded = MagicMock()
        loaded.policy = None
        loaded.agent_name = None
        loaded.api_key = None  # the keyless trigger
        loaded.local_cert = True
        sdk_config.load.return_value = loaded
        keyless_client = MagicMock()
        keyless_client.register_agent = AsyncMock(
            side_effect=AssertionError("no network call when keyless")
        )
        client_cls.from_config.return_value = keyless_client
        await caspase_hermes.async_register(ctx)

    plugin = caspase_hermes._current_plugin
    assert plugin is not None
    assert plugin._forced_offline is True
    assert plugin._state is not None and plugin._state.offline is True
    worker.assert_not_called()
    # The plugin asked for a keyless client rather than letting from_config raise.
    assert client_cls.from_config.call_args.kwargs.get("allow_keyless") is True
    registered = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert registered == VALID_HOOK_NAMES


# --- local death certificate on a kill --------------------------------------


async def test_offline_session_end_emits_local_cert_and_skips_post() -> None:
    """On an offline kill, session_end renders/saves the cert locally and does
    NOT POST it (there's no control plane to post to)."""
    plugin = CaspasePlugin(
        name="t", policy="coding-default", client=_offline_client(), local_cert=True
    )
    with patch("caspase_hermes.plugin.ensure_worker_started"):
        await plugin.setup()
    assert plugin._state is not None
    plugin._state.request_termination("loop test")

    with (
        patch.object(plugin, "_emit_local_cert") as emit,
        patch.object(plugin, "_post_death_cert_best_effort") as post,
    ):
        plugin.session_end()

    emit.assert_called_once()
    post.assert_not_called()


async def test_local_cert_disabled_suppresses_local_render() -> None:
    """local_cert=False → no local render even on a kill."""
    plugin = CaspasePlugin(
        name="t", policy="coding-default", client=_offline_client(), local_cert=False
    )
    with patch("caspase_hermes.plugin.ensure_worker_started"):
        await plugin.setup()
    assert plugin._state is not None
    plugin._state.request_termination("loop test")

    with patch.object(plugin, "_emit_local_cert") as emit:
        plugin.session_end()

    emit.assert_not_called()
