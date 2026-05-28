# Changelog

## 0.1.0a0 (2026-05-27)

Initial alpha release.

- **Apoptosis protocol core** — `WatcherState`, symptom checks, cooperative termination via the `terminate_requested` flag, and a `caspase.checkpoint()` entry point for custom loops
- **Six symptom checks** — loop induction, token runaway, cost cap, wall-clock cap, tool-scope violation, heartbeat loss; manual kill bypasses grants by design
- **Death certificate** — symptom log, shutdown sequence, cost summary, last-20 tool signature window, one-click feedback URL with single-use signed tokens
- **Hermes Agent plugin** (`caspase-hermes`) — drop-in supervision for Hermes v0.14. Installs via `pip install caspase-hermes` and is auto-discovered through the `hermes_agent.plugins` entry-point group. Hook callbacks attach to `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_api_request`, and `on_session_end`. Kill path is cooperative: when apoptosis fires, the plugin returns Hermes' canonical `{"action": "block", "message": ...}` directive from `pre_tool_call`, halting further tool execution while the agent's loop ends naturally. Death certificate is posted on `on_session_end`. This is the sole supported framework adapter for the alpha.
- **FastAPI control plane** with Postgres + Alembic — agent registry, event/heartbeat ingest, kill events, grants CRUD, `/healthz` with live `SELECT 1` DB probe
- **Operator CLI** — `caspase agents list`, `caspase logs <id>`, `caspase kill <id> --reason`, `caspase grants create/revoke` with worst-case latency banner
- **Grants system** — operator-only symptom suppression for a bounded window (max 24 h); validates symptom against the agent's resolved policy; rejects `manual_kill` unconditionally
- **Three shipped policies** — `strict` (3 repeats / $2 / 5 min, tight allowlist), `coding-default` (5 repeats / $25 / 30 min, grantable scope), `permissive` (10 repeats / $100 / 2 h, open tool surface)
- **End-to-end Hermes integration demo** — `uv sync` pulls Hermes Agent v0.14 into the workspace venv; `uv run python -m demo.coding_agent._run_control_plane` boots an in-process SQLite control plane on localhost:8000 (SQLAlchemy dialect patches for `JSONB`, `UUID`, `BigInteger`, `PG_UUID.bind_processor` let the Postgres-schema models stand up on SQLite without Alembic); a real `uv run hermes chat -q "..."` session is then supervised by Caspase and killed on the configured policy thresholds, with the death certificate visible via the operator CLI. Full walkthrough lives in the root README's "Try it" section.
- **Privacy by default** — only metadata leaves the agent process (tool name + argument hash, token counts, cost, model id); tool arguments and conversation transcripts are never sent
- **Bare-import contract** — `import caspase` works with zero third-party agent-framework dependencies
- **CI** — ruff + mypy strict + pytest on Postgres 15 + bare-import smoke test
- **Python 3.11+**, MIT licensed
