"""Caspase Hermes plugin — apoptosis supervision for Hermes Agent.

Installation
------------

    pip install caspase-hermes

Hermes auto-discovers the plugin via the ``hermes_agent.plugins`` entry-point
group declared in this package's ``pyproject.toml``. No directory copy is
required. Plugins are opt-in, so enable it by adding ``caspase`` to
``plugins.enabled`` in ``~/.hermes/config.yaml`` (Windows:
``%LOCALAPPDATA%\\hermes\\config.yaml``)::

    plugins:
      enabled:
        - caspase

Note: ``hermes plugins enable caspase`` and the interactive ``hermes plugins``
UI only manage git-installed plugins under ``~/.hermes/plugins/`` — they do not
list pip-installed (entry-point) plugins, which are enabled via the config key
above. The runtime loader (PluginManager.discover_and_load) still honours
``plugins.enabled`` for entry-point plugins.

For the legacy directory-install path, copy this package into
``~/.hermes/plugins/caspase/`` (``plugin.yaml`` is shipped alongside the
sources for that case).

Configuration (env vars or ``~/.hermes/.env``)
----------------------------------------------

    CASPASE_API_KEY      — your operator API key (required)
    CASPASE_BASE_URL     — control plane URL (default: http://localhost:8000)
    CASPASE_AGENT_NAME   — display name for this session (default: "hermes")
    CASPASE_POLICY       — policy name (default: "coding-default")

How it works
------------

Hermes calls ``register(ctx)`` once per session. The plugin attaches five
keyword-only hook callbacks against the real Hermes v0.14 hook API
(see ``hermes_cli/plugins.py``):

    pre_tool_call       — checkpoint; may return {"action": "block", ...}
    post_tool_call      — record outcome
    pre_llm_call        — lifecycle marker
    post_api_request    — token + cost accounting (carries the usage dict)
    on_session_end      — flush death cert, tear down worker

If an apoptosis condition fires (loop, cost, wall-clock, scope, manual kill),
the plugin's ``pre_tool_call`` returns Hermes' standard block directive on
every subsequent call. The agent gets a tool error response and the harm
is halted immediately — no further tool execution, no further cost. The
session ends cooperatively at the next natural turn boundary, at which
point ``on_session_end`` posts the death certificate.

Public surface
--------------

    register(ctx)         — Hermes plugin entry point (sync, called by runtime)
    async_register(ctx)   — async variant for callers inside a running loop
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from caspase.client import CaspaseClient
from caspase.config import SDKConfig

from caspase_hermes.plugin import CaspasePlugin

logger = logging.getLogger("caspase_hermes")

__version__ = "0.1.0a0"

_current_plugin: CaspasePlugin | None = None


def register(ctx: Any) -> None:
    """Hermes plugin entry point. Called once by the Hermes runtime at session start.

    ``ctx`` is the Hermes :class:`PluginContext` (v0.14). We use:
        ctx.register_hook(event_name, callback)   — wire lifecycle hooks
    No other ctx surface is required for the cooperative-kill design.
    """
    global _current_plugin

    name = os.environ.get("CASPASE_AGENT_NAME", "hermes")
    policy = os.environ.get("CASPASE_POLICY", "coding-default")

    config = SDKConfig.load()
    client = CaspaseClient.from_config(config)

    plugin = CaspasePlugin(name=name, policy=policy, client=client)

    # Run async setup synchronously — Hermes calls register() outside of an
    # async context. If an event loop is already running (e.g. in tests),
    # surface a RuntimeError pointing the caller at async_register().
    try:
        asyncio.get_running_loop()
        raise RuntimeError(
            "caspase_hermes.register() was called from inside a running event loop. "
            "If you are initialising Caspase from an async context, call "
            "await caspase_hermes.async_register(ctx) instead."
        )
    except RuntimeError as exc:
        if "no running event loop" in str(exc) or "no current event loop" in str(exc):
            asyncio.run(plugin.setup())
        else:
            raise

    _current_plugin = plugin

    _register_hooks(ctx)
    logger.info("caspase: plugin registered for session (agent=%r, policy=%r)", name, policy)


async def async_register(ctx: Any) -> None:
    """Async variant of register() for callers inside a running event loop."""
    global _current_plugin

    name = os.environ.get("CASPASE_AGENT_NAME", "hermes")
    policy = os.environ.get("CASPASE_POLICY", "coding-default")

    config = SDKConfig.load()
    client = CaspaseClient.from_config(config)

    plugin = CaspasePlugin(name=name, policy=policy, client=client)
    await plugin.setup()

    _current_plugin = plugin

    _register_hooks(ctx)
    logger.info("caspase: plugin async-registered for session (agent=%r, policy=%r)", name, policy)


def _register_hooks(ctx: Any) -> None:
    """Wire all five hook callbacks. Names match Hermes' VALID_HOOKS set."""
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    # post_api_request (NOT post_llm_call) carries the token-usage dict in
    # Hermes v0.14. post_llm_call's canonical payload is just
    # {session_id, model, platform} — no usage data — so it's useless for
    # cost tracking. See hermes_cli/hooks.py::_DEFAULT_PAYLOADS.
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("on_session_end", _on_session_end)


# --- hook dispatch -----------------------------------------------------------
# Hermes invokes hooks via cb(**kwargs) (plugins.py::invoke_hook line ~1559).
# All wrappers MUST be keyword-only and tolerate unknown kwargs via **_extra,
# so newer Hermes versions adding payload fields don't break us.


def _on_pre_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    **_extra: Any,
) -> dict[str, str] | None:
    """Hermes invokes this before every tool call.

    Returns ``{"action": "block", "message": ...}`` if apoptosis has fired
    (Hermes turns that into a tool error the agent sees); ``None`` otherwise.
    """
    if _current_plugin is None:
        return None
    return _current_plugin.pre_tool_call(tool_name=tool_name, args=args)


def _on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    duration_ms: float = 0,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    **_extra: Any,
) -> None:
    if _current_plugin is None:
        return
    _current_plugin.post_tool_call(tool_name=tool_name, args=args, result=result)


def _on_pre_llm_call(
    *,
    session_id: str = "",
    user_message: Any = None,
    conversation_history: Any = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    **_extra: Any,
) -> None:
    if _current_plugin is None:
        return
    _current_plugin.pre_llm_call(model=model)


def _on_post_api_request(
    *,
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    api_duration: float = 0,
    finish_reason: str = "",
    message_count: int = 0,
    response_model: str = "",
    usage: dict[str, Any] | None = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    **_extra: Any,
) -> None:
    if _current_plugin is None:
        return
    _current_plugin.post_api_request(
        model=model,
        usage=usage or {},
        api_duration=api_duration,
    )


def _on_session_end(*, session_id: str = "", **_extra: Any) -> None:
    if _current_plugin is None:
        return
    _current_plugin.session_end()
