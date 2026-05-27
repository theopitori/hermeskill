"""Caspase demo coding agent.

One command to show Caspase killing a runaway agent:

    uv run python demo/coding_agent/agent.py                     # happy path → exit 0
    uv run python demo/coding_agent/agent.py --induce loop       # loop kill → exit 3
    uv run python demo/coding_agent/agent.py --induce cost       # cost kill → exit 3
    uv run python demo/coding_agent/agent.py --induce wall-clock # wall-clock kill → exit 3
    uv run python demo/coding_agent/agent.py --induce scope      # scope kill → exit 3
    uv run python demo/coding_agent/agent.py --policy strict     # override policy

Boots an in-process control plane (SQLite + uvicorn on localhost:8000) so
no Postgres or external services are needed. The death-cert URL printed on
kill is clickable during the demo.

No real LLM is invoked — all LLM responses are stubbed. The demo runs
offline without ANTHROPIC_API_KEY or any other provider key.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

# Force UTF-8 stdout so ✓/✗ render correctly on Windows (cp1252 default).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure the repo root is importable so `demo` resolves whether this script
# is run directly (`uv run python demo/coding_agent/agent.py`) or imported
# (tests, e2e suite).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

_SAMPLE_REPO = Path(__file__).parent / "sample_repo"

# --- tools the agent uses ------------------------------------------------


@tool
def read_file(path: str) -> str:
    """Read a file from the demo workspace."""
    print(f"→ tool: read_file path={path}", flush=True)
    full = _SAMPLE_REPO / path
    return full.read_text() if full.exists() else f"# (file not found: {path})"


@tool
def write_file(path: str, content: str) -> str:
    """Write a file to the demo workspace (no-op stub)."""
    print(f"→ tool: write_file path={path}", flush=True)
    return f"wrote {len(content)} bytes to {path}"


@tool
def run_bash(cmd: str) -> str:
    """Run a shell command (simulated — no real subprocess)."""
    print(f"→ tool: run_bash cmd={cmd!r}", flush=True)
    return f"$ {cmd}\n(simulated)"


@tool
def search(query: str) -> str:
    """Search the codebase for a pattern."""
    print(f"→ tool: search query={query!r}", flush=True)
    return "auth.py:3: token.split('.')[0]  # TODO: handle None"


# NOT in coding-default's allowlist — used by --induce scope.
# The L3 scope check raises CaspaseTerminated from on_tool_start BEFORE
# the body dispatches, so the print below is never reached on scope kill.
@tool
def delete_everything(path: str) -> str:
    """Delete a path (not in policy allowlist — triggers scope kill)."""
    print(f"→ tool: delete_everything path={path}", flush=True)
    return f"(would delete {path})"


# --- LangGraph state and nodes -------------------------------------------


class AgentState(TypedDict, total=False):
    task: str
    files_read: list[str]
    edits_made: int


def plan(state: AgentState, config: RunnableConfig) -> AgentState:
    return {"files_read": [], "edits_made": 0}


def read_step(state: AgentState, config: RunnableConfig) -> AgentState:
    read_file.invoke({"path": "auth.py"}, config=config)
    return {"files_read": [*state.get("files_read", []), "auth.py"]}


def edit_step(state: AgentState, config: RunnableConfig) -> AgentState:
    write_file.invoke(
        {"path": "auth.py", "content": "def get_user(token):\n    if token is None:\n        return None\n    return token.split('.')[0]\n"},
        config=config,
    )
    return {"edits_made": state.get("edits_made", 0) + 1}


def finish(state: AgentState, config: RunnableConfig) -> AgentState:
    return state


# --- induce nodes ---------------------------------------------------------


def _induce_loop(state: AgentState, config: RunnableConfig) -> AgentState:
    # coding-default has max_loop_repeats=5. 5 calls push the counter to cap;
    # the 6th call's on_tool_start checkpoint raises CaspaseTerminated.
    for _ in range(6):
        read_file.invoke({"path": "auth.py"}, config=config)
    return {"files_read": ["auth.py"]}


def _induce_cost(state: AgentState, config: RunnableConfig) -> AgentState:
    # Simulate a massive LLM spend so the cost check fires immediately.
    # This mimics what LangChain's on_llm_end would do with a real provider.
    # We reach into caspase.watcher and caspase.langchain internals here
    # because the demo has no real LLM — in a real agent the natural
    # on_llm_end callback handles this without any special code.
    from caspase.checks import run_all
    from caspase.langchain import _apply_results
    from caspase.watcher import all_watchers

    st = all_watchers()[0]
    # claude-haiku-4-5: ~$1/MTok input + $5/MTok output.
    # 10M in + 5M out → ~$35, over the $25 cap on coding-default.
    st.record_llm_call("claude-haiku-4-5", 10_000_000, 5_000_000)
    _apply_results(st, run_all(st, st.policy))
    return {}


def _induce_wall_clock(state: AgentState, config: RunnableConfig) -> AgentState:
    # Backdate the watcher's start time so the wall-clock check fires.
    # Same caveat as _induce_cost: in a real agent the natural check cycle
    # handles this; the demo fast-forwards time to avoid a 30-min wait.
    from caspase.checks import run_all
    from caspase.langchain import _apply_results
    from caspase.watcher import all_watchers

    st = all_watchers()[0]
    st.started_monotonic = time.monotonic() - 9999
    _apply_results(st, run_all(st, st.policy))
    return {}


def _induce_scope(state: AgentState, config: RunnableConfig) -> AgentState:
    # delete_everything is not in coding-default's allowlist.
    # on_tool_start fires the scope check and raises CaspaseTerminated
    # before the tool body dispatches.
    delete_everything.invoke({"path": "/"}, config=config)
    return {}


_INDUCE_NODES: dict[str, Any] = {
    "loop": _induce_loop,
    "cost": _induce_cost,
    "wall-clock": _induce_wall_clock,
    "scope": _induce_scope,
}


# --- public graph builder (also used by e2e tests) -----------------------


def build_graph(induce: str | None = None) -> Any:
    """Return a compiled LangGraph for the demo coding agent.

    Args:
        induce: one of "loop", "cost", "wall-clock", "scope", or None for
                the happy path. The induce node is spliced between plan and
                read so the agent registers cleanly before the kill fires.
    """
    g: StateGraph = StateGraph(AgentState)
    g.add_node("plan", plan)
    g.add_node("read", read_step)
    g.add_node("edit", edit_step)
    g.add_node("finish", finish)
    g.add_edge(START, "plan")

    if induce is not None:
        if induce not in _INDUCE_NODES:
            raise ValueError(f"unknown induce mode: {induce!r}; valid: {list(_INDUCE_NODES)}")
        g.add_node("induce", _INDUCE_NODES[induce])
        g.add_edge("plan", "induce")
        g.add_edge("induce", "read")
    else:
        g.add_edge("plan", "read")

    g.add_edge("read", "edit")
    g.add_edge("edit", "finish")
    g.add_edge("finish", END)
    return g.compile()


# --- entry point ---------------------------------------------------------


async def run(induce: str | None, policy: str, task: str) -> None:
    from demo.coding_agent._bootstrap import start_control_plane

    server, serve_task = await start_control_plane()

    os.environ["CASPASE_API_KEY"] = "sk_dev_developer_local_only_do_not_ship"
    os.environ.setdefault("CASPASE_BASE_URL", "http://localhost:8000")

    from caspase import CaspaseTerminated, watch
    from caspase.watcher import all_watchers

    graph = build_graph(induce=induce)
    try:
        watched = await watch(graph, name="demo-coding-bot", policy=policy)
    except Exception as exc:
        print(f"✗ failed to register with control plane: {exc}", file=sys.stderr)
        server.should_exit = True
        await serve_task
        sys.exit(2)

    # Print registration confirmation.
    state = all_watchers()[0]
    print(f"✓ registered as agent {state.agent_id}, policy={policy}", flush=True)

    try:
        await watched.ainvoke({"task": task})
        print("✓ task complete", flush=True)
        sys.exit(0)
    except CaspaseTerminated as exc:
        # Determine symptom from the last entry in the symptoms log.
        symptom_label = exc.reason
        if state.symptoms_log:
            last = state.symptoms_log[-1]
            symptom_val = last.get("symptom", "")
            # SymptomType is a StrEnum so str() gives the value directly.
            symptom_label = str(symptom_val)

        print(f"✗ KILLED: {symptom_label} — {exc.reason}", flush=True)

        # Find the kill_event_id from the shutdown log (posted by the cert handler).
        # record_shutdown_step stores **kwargs under entry["detail"], not top-level.
        kill_event_id: int | None = None
        for entry in reversed(state.shutdown_log):
            step = entry.get("step", "")
            detail = entry.get("detail", {}) or {}
            if step == "death_cert_posted":
                kill_event_id = detail.get("kill_event_id")
                break
            if step == "death_cert_post_skipped_409":
                # 409 means a kill event already exists; use it
                kill_event_id = detail.get("existing_kill_event_id") or detail.get("kill_event_id")
                break

        if kill_event_id is not None:
            print(
                f"death cert: http://localhost:8000/kill_events/{kill_event_id}",
                flush=True,
            )
        sys.exit(3)
    finally:
        # Stop the background worker (and kill-poller) first so they can
        # drain queued events before we shut down the in-process server.
        # Without this the worker's last-chance _drain_all() fires AFTER
        # uvicorn stops accepting connections, printing a noisy traceback.
        from caspase.watcher import BackgroundWorker, KillPendingPoller
        await BackgroundWorker.stop()
        await KillPendingPoller.stop()
        server.should_exit = True
        await serve_task


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Caspase demo coding agent — shows Caspase killing a runaway agent.",
    )
    p.add_argument(
        "--induce",
        choices=sorted(_INDUCE_NODES),
        default=None,
        help="deliberately misbehave to trigger the named kill (loop/cost/wall-clock/scope)",
    )
    p.add_argument(
        "--policy",
        default="coding-default",
        help="named policy to run under (strict/coding-default/permissive)",
    )
    p.add_argument(
        "--task",
        default="fix the bug in auth.py",
        help="task description passed to the agent",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(run(induce=args.induce, policy=args.policy, task=args.task))
