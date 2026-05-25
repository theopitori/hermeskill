"""Caspase Hermes plugin — apoptosis supervision for Hermes Agent.

Install:

    pip install caspase-hermes
    cp -r "$(python -c "import caspase_hermes, pathlib; print(pathlib.Path(caspase_hermes.__file__).parent / 'plugin_dir')")" ~/.hermes/plugins/caspase/
    export CASPASE_API_KEY=sk-...
    hermes

Or point Hermes at this package directly:

    # ~/.hermes/config.yaml
    plugins:
      - path: /path/to/caspase-hermes/src/caspase_hermes

Hermes calls `register(ctx)` once per session. The plugin then supervises
every tool call and LLM call in that session, sending heartbeats and events
to the Caspase control plane. If the agent trips a runaway condition (loop,
cost, wall-clock, scope) or an operator issues a manual kill, the plugin
arms a `tool_override` kill stub that fires at the next tool boundary.

Configuration (env vars or .env file):

    CASPASE_API_KEY          — your operator API key
    CASPASE_CONTROL_PLANE_URL — defaults to http://localhost:8000
    CASPASE_AGENT_NAME       — display name for this session (default: "hermes")
    CASPASE_POLICY           — policy name (default: "coding-default")

Public surface:
    register(ctx)  — Hermes plugin entry point (called by the runtime)
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

    `ctx` is the Hermes plugin context object (v0.14). We use:
        ctx.register_hook(event_name, callback)
        ctx.tool_override(tool_name, replacement)  — armed lazily on kill
    """
    global _current_plugin

    name = os.environ.get("CASPASE_AGENT_NAME", "hermes")
    policy = os.environ.get("CASPASE_POLICY", "coding-default")

    config = SDKConfig.from_env()
    client = CaspaseClient.from_config(config)

    plugin = CaspasePlugin(ctx, name=name, policy=policy, client=client)

    # Run async setup synchronously — Hermes calls register() outside of an
    # async context. If an event loop is already running (e.g. in tests), use
    # asyncio.ensure_future and wait; otherwise use asyncio.run.
    try:
        loop = asyncio.get_running_loop()
        # Inside a running loop — schedule and wait using run_until_complete
        # on a *new* thread-loop pairing is not possible; instead we create
        # a task and let the caller's loop drive it.
        # Practical note: Hermes typically calls register() synchronously
        # before entering its async turn loop, so there should be no running
        # loop here. If this ever fires in an async context, the user will
        # see a RuntimeError with a clear message.
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

    # Wire hooks — Hermes calls these at the appropriate lifecycle points.
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)

    logger.info("caspase: plugin registered for session (agent=%r, policy=%r)", name, policy)


async def async_register(ctx: Any) -> None:
    """Async variant of register() for callers inside a running event loop."""
    global _current_plugin

    name = os.environ.get("CASPASE_AGENT_NAME", "hermes")
    policy = os.environ.get("CASPASE_POLICY", "coding-default")

    config = SDKConfig.from_env()
    client = CaspaseClient.from_config(config)

    plugin = CaspasePlugin(ctx, name=name, policy=policy, client=client)
    await plugin.setup()

    _current_plugin = plugin

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)

    logger.info("caspase: plugin async-registered for session (agent=%r, policy=%r)", name, policy)


# --- hook dispatch -----------------------------------------------------------
# These thin wrappers keep the ctx argument handling in one place and let
# the CaspasePlugin methods stay ctx-agnostic.


def _on_pre_tool_call(ctx: Any, tool_name: str, inputs: Any) -> None:
    if _current_plugin is not None:
        _current_plugin.pre_tool_call(tool_name, inputs)


def _on_post_tool_call(ctx: Any, tool_name: str, inputs: Any, output: Any) -> None:
    if _current_plugin is not None:
        _current_plugin.post_tool_call(tool_name, inputs, output)


def _on_pre_llm_call(ctx: Any, model: str, messages: Any) -> None:
    if _current_plugin is not None:
        _current_plugin.pre_llm_call(model, messages)


def _on_post_llm_call(ctx: Any, model: str, response: Any) -> None:
    if _current_plugin is not None:
        _current_plugin.post_llm_call(model, response)


def _on_session_end(ctx: Any) -> None:
    if _current_plugin is not None:
        _current_plugin.session_end()
