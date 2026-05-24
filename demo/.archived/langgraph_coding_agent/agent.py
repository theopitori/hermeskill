"""Demo agent: a tiny LangGraph that pretends to fix a bug.

Run against the live control plane:

    uv run python demo/coding_agent/agent.py
    uv run python demo/coding_agent/agent.py --induce loop
    uv run python demo/coding_agent/agent.py --induce cost
    uv run python demo/coding_agent/agent.py --induce wall_clock
    uv run python demo/coding_agent/agent.py --induce scope

Requires `.env` at repo root (or env vars) with:
    STASIS_DB_URL    — only used by the control plane, not this script
    STASIS_API_KEY   — the dev developer key

What it does:
    1. Defines a 4-node LangGraph: plan → read_file → write_file → finish
    2. Each working node calls a LangChain `@tool` so the StasisCallbackHandler
       sees `on_tool_start` and queues TOOL_CALL events
    3. Calls `await watch(graph, ...)` — the 5-line integration
    4. Runs `await graph.ainvoke(...)` to completion
    5. Prints the agent_id so you can verify with `stasis logs <id>` (M1.8)

**Induce modes (M2.6)** — deliberately misbehave to demonstrate that the
supervision catches each of the M2 symptoms. Each mode terminates the
agent cooperatively and leaves a death certificate on the control plane:

    --induce loop        — repeat read_file 6× → trips max_loop_repeats=5
    --induce cost        — fake $1000+ of LLM spend → trips max_cost_usd=25
    --induce wall_clock  — backdate started_monotonic 9999s → trips runtime
    --induce scope       — call a tool not in the policy allowlist

**Idle mode (M4)** — long-running well-behaved loop, intended for testing
manual kill. The agent registers and then sleeps in 0.5s ticks, calling
a tool each tick so the L1 checkpoint fires often. The SDK's kill-pending
poller picks up an operator-issued `/terminate` within ~3s and the next
checkpoint raises StasisTerminated.

    --idle               — loop until killed externally (or 120s timeout)

No real LLM is invoked — the goal here is to demonstrate the SDK plumbing
end-to-end, not exercise an LLM provider. M6 adds a real coding loop that
actually fixes a deliberately-broken sample repo.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from stasis_agent import StasisTerminated, watch
from stasis_agent.checks import run_all
from stasis_agent.client import AuthError
from stasis_agent.langchain import _apply_results
from stasis_agent.watcher import all_watchers

# --- minimal .env loader (no python-dotenv dep needed for the demo) -------


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# --- the "tools" the agent uses ------------------------------------------


@tool
def read_file(path: str) -> str:
    """Read a file from the workspace."""
    return f"# pretend contents of {path}\nprint('hello')\n"


@tool
def write_file(path: str, content: str) -> str:
    """Write a file to the workspace."""
    return f"wrote {len(content)} bytes to {path}"


# The `--induce scope` payload: a tool whose name is NOT in coding-default's
# allowlist (which is read_file, write_file, run_bash, search, http_get).
# `check_tool_scope` will reject it inside `on_tool_start` BEFORE the tool
# body runs — proving the L3 tool-quarantine mechanism.
@tool
def delete_everything(path: str) -> str:
    """A tool deliberately NOT in the allowlist — used by --induce scope."""
    # Body is never reached when the policy enforces scope; included only
    # so the @tool decorator has something to wrap.
    return f"would have deleted everything under {path}"


# --- LangGraph nodes ------------------------------------------------------


class AgentState(TypedDict, total=False):
    task: str
    files_read: list[str]
    edits_made: int


def plan(state: AgentState, config: RunnableConfig) -> AgentState:
    print(f"[plan] task = {state['task']!r}")
    return {"files_read": [], "edits_made": 0}


def read_step(state: AgentState, config: RunnableConfig) -> AgentState:
    # Tool invocation propagates the LangGraph config (which carries our
    # Stasis callback handler), so on_tool_start fires.
    contents = read_file.invoke({"path": "dummy.py"}, config=config)
    print(f"[read]  got {len(contents)} bytes")
    return {"files_read": [*state.get("files_read", []), "dummy.py"]}


def edit_step(state: AgentState, config: RunnableConfig) -> AgentState:
    write_file.invoke(
        {"path": "dummy.py", "content": "print('hello, world')\n"},
        config=config,
    )
    print("[edit]  applied fix")
    return {"edits_made": state.get("edits_made", 0) + 1}


def finish(state: AgentState, config: RunnableConfig) -> AgentState:
    print(f"[done] {state.get('edits_made', 0)} edit(s), files: {state.get('files_read')}")
    return state


# --- induce nodes (M2.6 — DoD verification) ------------------------------


def _induce_loop_step(state: AgentState, config: RunnableConfig) -> AgentState:
    """Trip max_loop_repeats by calling read_file 6× with identical args.

    Coding-default sets max_loop_repeats=5. The 5th identical call flips
    the apoptosis flag inside `on_tool_start`'s post-record `run_all`;
    the 6th call's pre-record `_checkpoint` raises `StasisTerminated`
    before the tool dispatches.
    """
    print("[induce loop] firing 6 identical read_file calls")
    for i in range(6):
        read_file.invoke({"path": "dummy.py"}, config=config)
        print(f"[induce loop]   call {i + 1}/6 ok")
    return {"files_read": ["dummy.py"]}


def _induce_cost_step(state: AgentState, config: RunnableConfig) -> AgentState:
    """Trip max_cost_usd by attributing a massive LLM call to the watcher.

    The demo has no real LLM, so we manually record LLM usage on the
    `WatcherState` and run the checks directly (in a production agent
    LangChain's `on_llm_end` would do both). The cost check returns
    Terminal; `_apply_results` flips the flag; the next chain boundary
    raises `StasisTerminated`.
    """
    print("[induce cost] attributing 15M tokens of fake LLM spend")
    state_obj = all_watchers()[0]
    # Anthropic claude-haiku-4-5 priced at ~$1/MTok input + $5/MTok output.
    # 10M input + 5M output → $35, well over the $25 cap.
    state_obj.record_llm_call("claude-haiku-4-5", 10_000_000, 5_000_000)
    _apply_results(state_obj, run_all(state_obj, state_obj.policy))
    return {}


def _induce_wall_clock_step(state: AgentState, config: RunnableConfig) -> AgentState:
    """Trip max_runtime_seconds by backdating the watcher's start time."""
    print("[induce wall_clock] backdating started_monotonic by 9999s")
    state_obj = all_watchers()[0]
    state_obj.started_monotonic = time.monotonic() - 9999
    _apply_results(state_obj, run_all(state_obj, state_obj.policy))
    return {}


def _induce_scope_step(state: AgentState, config: RunnableConfig) -> AgentState:
    """Trip tool-scope by invoking `delete_everything` (not in allowlist).

    The L3 tool quarantine raises StasisTerminated from inside
    `on_tool_start` — the `delete_everything` body never runs.
    """
    print("[induce scope] invoking delete_everything (not in allowlist)")
    delete_everything.invoke({"path": "/"}, config=config)
    return {}


_INDUCE_NODES = {
    "loop": _induce_loop_step,
    "cost": _induce_cost_step,
    "wall_clock": _induce_wall_clock_step,
    "scope": _induce_scope_step,
}


def _idle_step(state: AgentState, config: RunnableConfig) -> AgentState:
    """Long-running, well-behaved loop.

    For DoD step 7 (manual kill). Reads a file every 0.5s for up to 120s
    so an operator has plenty of time to issue `stasis kill`. Each
    iteration goes through `read_file.invoke(...)` which fires
    `on_tool_start` → L1 checkpoint, so the kill poller's flag flip is
    observed within roughly one tick.
    """
    print("[idle] looping; waiting to be killed", flush=True)
    deadline = time.monotonic() + 120.0
    i = 0
    while time.monotonic() < deadline:
        # Mix the iteration into the tool args so we don't trip the loop
        # detector — different signatures each tick.
        read_file.invoke({"path": f"idle-tick-{i}.txt"}, config=config)
        time.sleep(0.5)
        i += 1
    print("[idle] timed out without being killed", flush=True)
    return {"files_read": [f"idle-tick-{i}.txt"]}


def build_graph(induce: str | None = None, idle: bool = False) -> Any:
    """Build the demo graph; if `induce` is set, splice in a misbehaving node.

    The misbehavior node sits between `plan` and `read` — that placement
    means the agent gets to register cleanly and post some normal events
    before the kill fires, which makes the death cert's symptoms_log +
    shutdown_log more representative of a real production kill.
    """
    g = StateGraph(AgentState)
    g.add_node("plan", plan)
    g.add_node("read", read_step)
    g.add_node("edit", edit_step)
    g.add_node("finish", finish)

    g.add_edge(START, "plan")
    if idle:
        # Idle mode: replace the working nodes with a long-running loop
        # that waits for the kill poller to flip the apoptosis flag.
        g.add_node("idle", _idle_step)
        g.add_edge("plan", "idle")
        g.add_edge("idle", "finish")
    elif induce:
        if induce not in _INDUCE_NODES:
            raise ValueError(f"unknown induce mode: {induce!r}")
        g.add_node("induce", _INDUCE_NODES[induce])
        g.add_edge("plan", "induce")
        g.add_edge("induce", "read")
        g.add_edge("read", "edit")
        g.add_edge("edit", "finish")
    else:
        g.add_edge("plan", "read")
        g.add_edge("read", "edit")
        g.add_edge("edit", "finish")
    g.add_edge("finish", END)
    return g.compile()


# --- entry point ---------------------------------------------------------


async def run(
    task: str = "fix the bug in dummy.py",
    induce: str | None = None,
    idle: bool = False,
) -> None:
    _load_dotenv()
    graph = build_graph(induce=induce, idle=idle)
    if idle:
        agent_name = "demo-coding-bot-idle"
    elif induce:
        agent_name = f"demo-coding-bot-induce-{induce}"
    else:
        agent_name = "demo-coding-bot"
    try:
        watched = await watch(
            graph,
            name=agent_name,
            policy="coding-default",
            metadata={"demo": True, "induce": induce, "idle": idle},
        )
    except AuthError as exc:
        print(f"\nauth error: {exc}", file=sys.stderr)
        print(
            "\nCreate a `.env` in the repo root with at least:\n"
            "  STASIS_API_KEY=sk_dev_developer_local_only_do_not_ship\n"
            "  STASIS_BASE_URL=http://localhost:8000\n",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        result = await watched.ainvoke({"task": task})
    except StasisTerminated as exc:
        # Expected when `--induce` is used. The wrapper has already
        # posted the death certificate before re-raising.
        print(f"\n!! agent terminated: {exc.reason}", file=sys.stderr)
        for w in all_watchers():
            print(
                f"\ntip: read the death cert:\n  uv run stasis logs {w.agent_id}",
                file=sys.stderr,
            )
        sys.exit(3)

    # Allow the BackgroundWorker one more tick to flush events before we
    # exit (the worker drains pending events on shutdown anyway, but this
    # gives the heartbeat post a moment to land too).
    print(f"\nfinal state: {result}")
    print("\ntip: see what the control plane saw with:")
    for w in all_watchers():
        print(f"  uv run stasis logs {w.agent_id}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stasis demo coding agent. Optionally induce a kill for DoD verification.",
    )
    p.add_argument(
        "--task",
        default="fix the bug in dummy.py",
        help="task to hand to the agent (default: fix the bug)",
    )
    p.add_argument(
        "--induce",
        choices=sorted(_INDUCE_NODES.keys()),
        default=None,
        help="deliberately misbehave to trip the named symptom check",
    )
    p.add_argument(
        "--idle",
        action="store_true",
        help="run a long-lived idle loop (for testing manual kill)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(run(task=args.task, induce=args.induce, idle=args.idle))
