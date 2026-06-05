# Changelog

## Unreleased

- **`hermeskill monitor` — live agent vitals in your terminal.** The real-time
  counterpart to the death certificate: run it in a second terminal beside
  `hermes chat` and watch the agent's vitals tick — cost climbing toward the cap,
  loop-pressure gauge filling, wall-clock advancing — then, when apoptosis fires,
  the panel goes red and **flatlines** with the kill reason and (when
  `local_cert` is on) the death certificate spliced in. Works with **no control
  plane and no API key**: the Hermes plugin writes a small vitals snapshot to
  `~/.hermeskill/live/<agent_id>.json` on every hook boundary (best-effort,
  fail-open; disable with `HERMESKILL_LIVE=0`), and the monitor tails it. It's the keyless sibling of
  `hermeskill logs --follow`. New `hermeskill.vitals` module owns the snapshot
  schema + atomic read/write; `bridge.py` stays pure (the producer lives in the
  plugin). The loop-pressure gauge reuses `check_loop`'s exact count, so it can
  never disagree with the trigger.
- **Removed the offline demo** — the `demo/` package (`python -m demo` and its scenarios), its smoke tests, the `demo.tape`/GIF tooling, and the SQLite "Try it" walkthrough are gone. They were scaffolding to exercise the engine without a real agent and were never meant to ship; Hermeskill's value is supervising a real Hermes session. The control-plane section now documents the real Postgres boot, and [docs/real-kill.md](docs/real-kill.md) is the verbatim proof. The `python -m demo…` reference under 0.1.0a0 below no longer applies.

## 0.1.0a1 (2026-05-31)

Zero-config, no-control-plane supervision — the death certificate now lands on every kill, even with no API key and no backend running.

- **Keyless local-only path** — the Hermes plugin no longer requires `HERMESKILL_API_KEY`. When no key is configured it runs fully in-process (`forced_offline`): in-process symptom checks + the L1 cooperative block directive supervise the agent and the kill fires without any network call. `HERMESKILL_API_KEY` is now **optional** and only enables control-plane archival, fleet visibility, manual kill, and grants. (`hermeskill.HermeskillClient.from_config(..., allow_keyless=True)`; the operator CLI stays strict and still errors cleanly without a key.)
- **Local death certificate** — new `hermeskill.certificate` module renders the death certificate to a plain-text box and saves it to `~/.hermeskill/kills/<agent_id>-<timestamp>.txt` (+ `.json`) on every kill. On by default; toggle with `HERMESKILL_LOCAL_CERT=0` or `local_cert = false` in `config.toml`. The autopsy is now delivered in the zero-config path, not just when a control plane is reachable.
- **`hermeskill enable-hermes`** — one-shot command that flips `hermeskill` in your Hermes `plugins.enabled` (with `--disable` to undo). Replaces the documented hand-edit of the Hermes config; `hermes plugins enable` does not manage entry-point plugins. Idempotent; exits cleanly when Hermes isn't installed.
- **No offline traceback spam** — the background worker / kill poller no longer boot when running offline, so a keyless session no longer logs a full traceback every few seconds against an unreachable control plane.
- **`hermeskill init --local-cert/--no-local-cert`** — surfaces the local-cert toggle when writing `~/.hermeskill/config.toml`.
- **Docs** — README now leads with a 60-second zero-config quickstart (`install → hermeskill enable-hermes → hermes`); control plane is reframed as an opt-in "level up" for persistent history, fleet visibility, manual kill, and grants.

## 0.1.0a0 (2026-05-27)

Initial alpha release.

- **Apoptosis protocol core** — `WatcherState`, symptom checks, cooperative termination via the `terminate_requested` flag, and a `hermeskill.checkpoint()` entry point for custom loops
- **Six symptom checks** — loop induction, token runaway, cost cap, wall-clock cap, tool-scope violation, heartbeat loss; manual kill bypasses grants by design
- **Death certificate** — symptom log, shutdown sequence, cost summary, last-20 tool signature window, one-click feedback URL with single-use signed tokens
- **Hermes Agent plugin** (`hermeskill-hermes`) — drop-in supervision for Hermes v0.14. Installs via `pip install hermeskill-hermes` and is auto-discovered through the `hermes_agent.plugins` entry-point group. Hook callbacks attach to `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_api_request`, and `on_session_end`. Kill path is cooperative: when apoptosis fires, the plugin returns Hermes' canonical `{"action": "block", "message": ...}` directive from `pre_tool_call`, halting further tool execution while the agent's loop ends naturally. Death certificate is posted on `on_session_end`. This is the sole supported framework adapter for the alpha.
- **FastAPI control plane** with Postgres + Alembic — agent registry, event/heartbeat ingest, kill events, grants CRUD, `/healthz` with live `SELECT 1` DB probe
- **Operator CLI** — `hermeskill fleet`, `hermeskill logs <id>`, `hermeskill kill <id> --reason`, `hermeskill grant`/`hermeskill revoke` with worst-case latency banner
- **Grants system** — operator-only symptom suppression for a bounded window (max 24 h); validates symptom against the agent's resolved policy; rejects `manual_kill` unconditionally
- **Three shipped policies** — `strict` (3 repeats / $2 / 5 min, tight allowlist), `coding-default` (5 repeats / $25 / 30 min, grantable scope), `permissive` (10 repeats / $100 / 2 h, open tool surface)
- **End-to-end Hermes integration demo** — `uv sync` pulls Hermes Agent v0.14 into the workspace venv; `uv run python -m demo.coding_agent._run_control_plane` boots an in-process SQLite control plane on localhost:8000 (SQLAlchemy dialect patches for `JSONB`, `UUID`, `BigInteger`, `PG_UUID.bind_processor` let the Postgres-schema models stand up on SQLite without Alembic); a real `uv run hermes chat -q "..."` session is then supervised by Hermeskill and killed on the configured policy thresholds, with the death certificate visible via the operator CLI. Full walkthrough lives in the root README's "Try it" section.
- **Privacy by default** — only metadata leaves the agent process (tool name + argument hash, token counts, cost, model id); tool arguments and conversation transcripts are never sent
- **Bare-import contract** — `import hermeskill` works with zero third-party agent-framework dependencies
- **CI** — ruff + mypy strict + pytest on Postgres 15 + bare-import smoke test
- **Python 3.11+**, MIT licensed
