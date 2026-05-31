# caspase-langgraph

Apoptosis supervision for **LangGraph / LangChain** agents. Wrap your compiled
graph and Caspase watches it — loop, cost, wall-clock, and tool-scope checks
run at every node and tool boundary, and a runaway agent is cooperatively
terminated with an auditable death certificate.

```bash
pip install "caspase-langgraph[graph]"
```

```python
from caspase_langgraph import watch

async def main():
    # my_graph is a compiled LangGraph (StateGraph.compile()) or any Runnable
    graph = await watch(my_graph, name="bot-v1", policy="coding-default")
    await graph.ainvoke({"task": "fix the failing test"})
```

Set `CASPASE_API_KEY` (and `CASPASE_BASE_URL` if your control plane isn't on
`localhost:8000`) via env or `.env`.

## How the kill works here

LangChain has no "block this tool call" return channel like Hermes hooks do.
The documented way to abort is to **raise** from a callback — so when a
terminal symptom fires, `CaspaseCallbackHandler` raises `CaspaseTerminated` at
the next node/tool boundary (with `raise_error = True` so LangChain propagates
it instead of swallowing it). `watch()` catches that, posts the death
certificate, and re-raises so your own `try/finally` cleanup still runs.

This is the **cooperative (L1)** kill — the agent is stopped at the next
boundary, not force-killed mid-CPU. For OS-level hard-kill of a wedged child
process, see the core SDK's `ProcessSupervisor`.

## Why a separate package

The core `caspase` SDK has no LangChain dependency. Each runtime gets a thin,
opt-in adapter package (`caspase-hermes`, `caspase-langgraph`) so installing
the SDK stays light and the engine stays runtime-agnostic. The adapter only
translates lifecycle events into `WatcherState` mutations; all the detection
logic lives in `caspase`.
