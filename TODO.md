# Stasis TODO — known issues to address as we hit them

These are real concerns flagged by the user during plan review. They are **not**
in the plan file by design — they're things to fix at the moment we touch the
relevant code, not architectural axioms.

Address in-place as you hit each surface. Tick when done.

---

## [ ] #2 — Sync-call wedge: `task.cancel()` is half-true even with async

`task.cancel()` only fires `CancelledError` at the next `await`. If the agent is
stuck in synchronous code (`subprocess.run` that's hung, sync `time.sleep`,
CPU-bound parsing) cancel does nothing until the sync returns. `run_bash` is in
the default policy — bash commands hang all the time.

**Fix when we get to M2:**
- Add **L2.5 subprocess kill escalation** to `apoptosis.py`. If the agent runs
  in a subprocess (recommended deploy posture), SIGTERM → wait → SIGKILL the
  subprocess. If it's in-process, document that we're at the mercy of Python's
  cooperative cancellation.
- The demo agent (M1/M6) should run in its **own subprocess** so the death is
  observable and clean. Don't run it inside the control-plane process.

---

## [x] #4 — Manual-kill latency math is bad UX in the default config — **PARTIAL (M4)**

Landed in M4:
- `stasis kill` now prints the worst-case latency banner up front
  (`policy=… worst-case cooperative kill latency = 43s (poll + grace +
  verification). CLI timeout: 86s.`) so the user has the right mental
  model before the wait starts.
- Staged progress: `✓ kill issued (kill_event=…)` → `… cooperative
  shutdown` (on `dying`) → `✓ confirmed dead (elapsed N.Ns)`.
- Timeout path exits 6 with a "kill issued but unconfirmed within Xs"
  message naming the kill_event id, instead of polling forever.
- CLI poll cadence (`--poll-interval`, default 0.5s) is independent of
  the agent's policy poll interval — it's purely a display knob.

Deferred:
- **Per-policy `kill_poll_interval_seconds`.** Still a process-wide SDK
  default (`DEFAULT_KILL_POLL_INTERVAL = 3`). Customers who want 1s
  polling can override at `ensure_worker_started(kill_poll_interval=…)`;
  driving it off `min(policy.thresholds.kill_poll_interval)` across the
  registry is a follow-up if anyone asks. Not blocking on it because
  no user wants this yet, and the per-policy bit creates a fan-in
  problem (which policy wins?) that's better postponed than guessed.
- **Server-side INITIATED→CONFIRMED/ZOMBIE sweeper.** Mentioned in the
  `KillEventStatus` docstring as "Set by the server-side sweeper (M4)"
  but cut from MVP — the happy path posts the cert via M2.5's UPDATE
  which promotes status. The sweeper is only needed when the SDK
  *never* posts (process crash mid-kill), which is the zombie case the
  CLI's exit-6 message already surfaces to the operator.

---

## [ ] #5 — Pricing table will rot fast; make it fail soft

**Fix when we build `pricing.py` (M2):**
- Add `last_updated: date` per entry; warn at watcher init if any entry is
  >30 days old.
- Support customer-overridable pricing via `policy.pricing_overrides[model] = …`
  for negotiated rates.
- **Fail soft on missing model**: if a model isn't in the table, log a
  `pricing_unknown` warning event and **skip** the cost check. Do NOT crash the
  watcher. Loop / wall-clock / heartbeat checks must still run.

---

## [x] #6 — PyPI namespace check: does `stasis` exist? — **RESOLVED**

`stasis` v0.2 exists on PyPI. Cannot claim it.

**Decision:**
- **Distribution name (PyPI):** `stasis-agent`
- **Import name (Python):** `stasis_agent` (avoids collision if user has v0.2 `stasis` installed)
- **5-line example** becomes:
  ```python
  from stasis_agent import watch
  from my_agent import graph

  graph = watch(graph, name="coding-bot-v1", policy="coding-default")
  await graph.ainvoke({"task": "fix the bug"})
  ```
- CLI entry point stays `stasis` (separate namespace from Python imports, no conflict).

---

## [ ] #7 — Demo agent must do something real, not a toy

`demo/coding_agent/` is what early customers play with first. A toy that calls
tools to satisfy the demo will undersell the product.

**Fix when we build M1 (skeleton agent) and M6 (DoD demo):**
- Real coding agent: reads files, runs shell commands, makes edits.
- Operates on a sample repo with a **deliberately-introduced bug**.
- Loop-induction failure mode: "make the same wrong edit 6 times" — a realistic
  agent failure, not a synthetic test trigger.
- Wall-clock / token-runaway inductions should also be realistic
  (e.g., infinite "explore the repo" search).

---

## [ ] #8 — Concurrent agents in one Python process

A customer running 50 agents in parallel inside one process should not get 50
heartbeat tasks. Bake this in from M1, not later.

**Architectural rule (lock in at M1):**
- **One `WatcherState` per `watch()` call** (per agent instance).
- **One shared `HeartbeatBatcher`** task per process, registered in a
  module-level singleton. It enumerates all live `WatcherState`s every
  `heartbeat_interval_seconds` and POSTs a batched
  `[{agent_id, last_heartbeat_at, …}]` to a new endpoint `POST /heartbeats/batch`.
- Same pattern for telemetry event upload: shared queue, one drainer.
- Same pattern for the kill-pending poller: one polling task that asks the
  control plane for kill directives for *all* registered agents in this process
  via a batch endpoint.

Document this in `docs/apoptosis-protocol.md` so customers understand the
fan-out model.

---

## [x] #9 — Feedback token hashing must be symmetric — **RESOLVED**

Landed in M3:
- `control_plane.feedback_tokens.hash_feedback_token(raw)` is the single
  SHA-256 helper. Used at issue time (POST /agents/{id}/kill_events) and at
  lookup time (POST /feedback/{token}) — symmetric on both sides.
- `generate_feedback_token()` returns `(raw, hash)`; only the hash is
  persisted. Raw lives only in the cert's `feedback_url`.
- `test_feedback.py::test_feedback_round_trip_updates_kill_event` walks the
  full path: mint cert → extract URL → POST raw token → assert
  `kill_events.feedback_label` + `feedback_at` updated.
- Single-use (`used_at` → 410) and expiry (`expires_at` → 404) covered too.

---

## [ ] #10 — MCP endpoint: expand or cut

Currently `/mcp` exposes only `stasis_kill(agent_id, reason)`. A supervisor
agent can kill but can't see — killing blind.

**Decision point at M6:**
- Either expand to `stasis_fleet()`, `stasis_logs(agent_id)`, `stasis_grant(...)`
  — making it a real supervisor API.
- Or **cut MCP from MVP entirely**. It doesn't appear in any DoD step. User
  leans toward cutting; lean toward cutting unless cheap to do well.

Default action: **cut from MVP**, add a one-liner in `docs/README.md` saying
MCP is v1.1. Reclaim the M6 budget for polish.

---

## [ ] #11 — `/healthz` should exercise the DB pool from M1 onward

Right now `/healthz` returns a static JSON. Fine for M0 (no DB yet), but the
moment M1 lands the DB connection, `/healthz` must do a `SELECT 1` through the
async engine. Catches connectivity issues before customers do.

**Fix when M1 wires the DB:**
- `/healthz` runs `SELECT 1` via `SessionLocal()`; returns 503 with a small
  JSON body on failure.
- Keep the existing static fields (`status`, `version`) for backwards-compat;
  add `db: "ok"|"error"` and a `checked_at` timestamp.
- Smoke test asserts both the 200 happy path and the 503 path (mock the
  engine to raise).

---

## [ ] #12 — Per-milestone Ubuntu VM deploy cadence

Solo Windows dev → Linux deploy gap is the classic source of late
"works on my machine" surprises (path separators, default async event loop
policy, `uvloop` not being on Windows, `os.name == 'nt'` branches we miss).

**Habit to lock in starting at M1:**
- After each milestone is green locally, deploy to a throwaway Ubuntu VM
  (cheap cloud VM or local Hyper-V/WSL2 image) using `deploy/setup.sh`.
- Run the milestone's demo against the VM, not localhost.
- Drift caught in M1 is a 10-minute fix; drift caught in M6 is a week of
  hunting Windows-isms.

(Don't bake this into CI yet — manual cadence is enough until M3. Add a GH
Actions matrix job in M3 once we have meaningful integration coverage.)

---

## [ ] #13 — Set up `pgpass.conf` at M1 start (Option 3b)

Don't keep retyping the Postgres dev password or hardcoding it in scripts.
At M1 kickoff:

```
%APPDATA%\postgresql\pgpass.conf
  localhost:5432:*:postgres:<SUPERUSER_PASS>
  localhost:5432:*:stasis:<DEV_PASS>
```
File must be **mode-restricted** on Windows (set ACLs to user-only). Then
`psql -U postgres` and any tooling auto-authenticate. Standard Postgres dev
pattern; 60 seconds of setup, never think about it again.

---

## Decisions locked (record-keeping for future-me)

### Auth model — Option (b): real `api_keys` table from M1

When auth lands in M1, the first migration creates `customers` + `api_keys`
tables and **seeds a dev developer key + a dev operator key**. The middleware
does a real hashed-key DB lookup from day one — no stub key, no
`STASIS_DEV_KEY` env-var path that drifts from prod.

Tests exercise the real auth path. The dev keys live in a `.env` file
(gitignored) for local use.

### Shared types — Option (a) implicit: SDK as source of truth

Pydantic models in `packages/stasis-sdk/src/stasis_agent/types.py` are the
single source of truth. The control plane's `pyproject.toml` already depends
on `stasis-agent` (workspace dep), so `from stasis_agent.types import ...`
is the canonical import path on the server side too.

**Rule:** new shared schemas go in `stasis_agent.types`. Control-plane-only
DTOs (e.g. request/response shapes that never appear in the SDK) can live in
`control_plane.api.<router>.schemas` to keep the SDK surface small.

If this starts straining at M3 (e.g. the SDK doesn't want to carry server
internals), extract a third `stasis-types` package — but don't pre-factor.

### Dev DBs — two of them

Local dev creates **both** `stasis` and `stasis_test` databases up front,
both owned by the `stasis` role. The test DB is needed by M1 integration
tests anyway — creating it now saves a context switch later.

---


## Resolved

- **#4** — Manual-kill staged progress + worst-case latency banner (M4). Per-policy poll + server sweeper deferred; see section above.
- **#6** — PyPI namespace: dist `stasis-agent`, import `stasis_agent`, CLI `stasis`.
- **#9** — Feedback token hashing symmetric (M3). See section above.
- **#11** — `/healthz` upgraded to `SELECT 1` probe in M1.2, returns 503 on DB failure with `db_error` field.
- **#14** — Windows event-loop shim removed entirely in M1.3 by switching `psycopg[binary]` → `asyncpg`. asyncpg works on `ProactorEventLoop` so no `SelectorEventLoop` workaround needed; deleted from `main.py`, `env.py`, and `conftest.py`. The deprecated `set_event_loop_policy` is gone from the codebase.
