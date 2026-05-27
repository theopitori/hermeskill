# Changelog

## 0.1.0a0 (2026-05-27)

Initial alpha release.

- **Apoptosis protocol core** — `WatcherState`, symptom checks, cooperative termination via the `terminate_requested` flag, and a `caspase.checkpoint()` entry point for custom loops
- **Six symptom checks** — loop induction, token runaway, cost cap, wall-clock cap, tool-scope violation, heartbeat loss; manual kill bypasses grants by design
- **Death certificate** — symptom log, shutdown sequence, cost summary, last-20 tool signature window, one-click feedback URL with single-use signed tokens
- **Hermes Agent plugin** (`caspase-hermes`) — drop-in supervision via `tool_override` kill stub; non-blocking hooks; auto-activates on Hermes session start
- **LangGraph adapter** quarantined behind the `[langgraph]` extra — callback handler raises `CaspaseTerminated` at the next tool boundary; tests under `@pytest.mark.legacy`
- **FastAPI control plane** with Postgres + Alembic — agent registry, event/heartbeat ingest, kill events, grants CRUD, `/healthz` with live `SELECT 1` DB probe
- **Operator CLI** — `caspase agents list`, `caspase logs <id>`, `caspase kill <id> --reason`, `caspase grants create/revoke` with worst-case latency banner
- **Grants system** — operator-only symptom suppression for a bounded window (max 24 h); validates symptom against the agent's resolved policy; rejects `manual_kill` unconditionally
- **Three shipped policies** — `strict` (3 repeats / $2 / 5 min, tight allowlist), `coding-default` (5 repeats / $25 / 30 min, grantable scope), `permissive` (10 repeats / $100 / 2 h, open tool surface)
- **Demo coding agent** — `uv run python demo/coding_agent/agent.py [--induce loop|cost|wall-clock|scope]` boots an in-process SQLite control plane on localhost:8000 and prints a clickable death-cert URL on kill; runs offline with no LLM key
- **In-process SQLite control plane** for the demo — SQLAlchemy dialect patches (`JSONB`, `UUID`, `BigInteger`, `PG_UUID.bind_processor`) so the Postgres-schema models stand up on SQLite without Alembic
- **Privacy by default** — only metadata leaves the agent process (tool name + argument hash, token counts, cost, model id); tool arguments and conversation transcripts are never sent
- **Bare-import contract** — `import caspase` works with zero third-party agent-framework dependencies; LangGraph/LangChain imports are lazy and gated behind `watch()` on a LangGraph object
- **CI** — ruff + mypy strict + pytest on Postgres 15 + bare-import smoke test; 207 SDK and Hermes tests
- **Python 3.11+**, MIT licensed
