# Hermeskill — an apoptosis protocol for AI agents

Install Hermeskill into your agent and it watches every tool call and LLM turn. The
moment the agent loops, blows its budget, runs too long, or reaches for a tool
it was never scoped to, Hermeskill **kills it and files an auditable death
certificate** — a forensic record of exactly why it died.

Built for **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — a
drop-in plugin, no glue code.

## Quickstart (60 seconds, no control plane, no API key)

Supervision runs **in-process** — no server to stand up, no key to set, nothing
to configure.

```bash
# 1. Install Hermes with the Hermeskill plugin in its environment:
uv tool install hermes-agent --with hermeskill-hermes

# 2. Install the Hermeskill CLI. The `--with hermes-agent` lets `enable-hermes`
#    read and edit Hermes' config; the CLI's own commands stay independent.
uv tool install hermeskill --with hermes-agent

# 3. Put uv's tool directory on PATH (one-time), then restart your shell:
uv tool update-shell

# 4. Enable the plugin (one shot — flips plugins.enabled in your Hermes config):
hermeskill enable-hermes

# 5. Run Hermes as usual. Hermeskill now watches every session:
hermes
```

> Why two installs? `uv tool install hermes-agent` only exposes Hermes'
> executables, not the `hermeskill` CLI that ships with the `hermeskill` dependency —
> so the CLI needs its own `uv tool install`. (A leaner `uv tool install hermeskill`
> without `--with hermes-agent` works for every command *except* `enable-hermes`,
> which needs to import Hermes to locate its config.)

When an agent goes rogue — loops, blows its budget, runs too long, reaches for
an off-limits tool — Hermeskill kills it and **prints a death certificate to your
terminal, saving a copy to `~/.hermeskill/kills/`**:

```text
┌─ DEATH CERTIFICATE ───────────────────────────────────
│ agent      coding-agent-7
│ trigger    auto / loop
│ reason     read_file repeated 3x in last 3 actions (cap 3)
│ symptoms   1 terminal  ·  loop
│ shutdown   apoptosis_requested → block directive
│ cost       $0.42  ·  18.2k in / 2.1k out
└───────────────────────────────────────────────────────
```

Proof it's real: a [Hermes + GPT-4o kill, verbatim](docs/real-kill.md) — an
unscripted run, no edits.

That's the whole main function — **kill + autopsy, zero config**. The symptom
checks (loop, cost, wall-clock, tool-scope) run entirely in your agent's
process and need no network. Running a control plane is an **optional level-up**
([below](#level-up-run-a-control-plane)) that adds persistent/queryable history,
fleet visibility across agents, operator-issued manual kills, and grants — turn
it off and you still get the kill and the certificate.

## What the kill actually does

Hermeskill's termination is **cooperative by default**, escalating through three
layers:

- **L1 — block directive.** On a terminal symptom the framework adapter returns
  `{"action": "block", "message": "hermeskill apoptosis: …"}` on every subsequent
  tool call. The agent is *asked* to stop; its loop winds down naturally.
- **L2 — watchdog.** A per-agent daemon thread ([`apoptosis.py`](packages/hermeskill-sdk/src/hermeskill/apoptosis.py))
  escalates after a grace window with `loop.call_soon_threadsafe(task.cancel)`,
  cancelling the agent's asyncio **task** from outside its event loop.
- **L3 — process supervisor (opt-in).** [`ProcessSupervisor`](packages/hermeskill-sdk/src/hermeskill/supervisor.py)
  runs the agent in a **child process** and escalates **SIGTERM → grace →
  SIGKILL** from the parent. Because it lives in a separate process, it kills an
  agent the agent cannot veto.

**The boundary, stated honestly.** L1/L2 cannot stop an agent wedged in
CPU-bound or synchronous code — both rely on the event loop reaching an `await`,
and they cancel a *task*, not an OS *process*. **L3 is the answer to exactly
that case**, and it's the layer that makes "apoptosis" literally true. It's
opt-in (the cooperative path stays the default for well-behaved agents), and on
Windows `terminate()` is already a hard kill (there is no catchable SIGTERM), so
the grace window applies on POSIX only.

Whichever layer fires, the death certificate records the exact shutdown sequence
(`supervisor_sigterm`, `supervisor_sigkill`, …) — the certificate is only as
trustworthy as the claims around it.

---

## What you get

```
hermeskill/
├── control plane     FastAPI service that stores agents, policies, kill events, grants
├── SDK               watcher state, symptom checks, death certificates, kill client
├── Hermes plugin     drop-in supervision for Hermes Agent
└── operator CLI      list agents, tail logs, issue kills, grant exceptions
```

For each watched agent you get:

- **Live symptom checks** — loop induction, token-runaway, cost cap, wall-clock cap, tool-scope violation, heartbeat loss.
- **A death certificate** — symptom log, shutdown sequence, one-click feedback URL the operator can label.
- **A grant system** — operators can suppress one symptom for a bounded window when a kill would be wrong.

---

## Prerequisites

| Requirement | Minimum | Check | Install |
|---|---|---|---|
| Python | 3.11+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| uv *(recommended)* | any | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Postgres *(control plane only)* | 14+ | `psql --version` | [postgresql.org](https://www.postgresql.org/download/) |

**Windows quick install:**
```powershell
winget install astral-sh.uv
```

**macOS quick install:**
```bash
brew install python@3.11 uv
```

**Ubuntu/Debian:**
```bash
sudo apt install python3.11 python3-pip
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Level up: run a control plane

The [Quickstart](#quickstart-60-seconds-no-control-plane-no-api-key) already
kills runaway agents and hands you a death certificate. A **control plane** adds
the operator layer on top: persistent, queryable kill history; a live fleet view
across every agent; operator-issued **manual kills**; and **grants** to
pre-authorize a kill that would otherwise be wrong. Nothing below changes the
kill itself — it makes the kills *visible and governable across a team*.

The control plane is a FastAPI service backed by Postgres that you run yourself.
Bring it up locally:

```bash
# 1. Create / point at a dev Postgres (deploy/dev-db-bootstrap.ps1 on Windows,
#    deploy/setup.sh on Ubuntu) and export its DSN:
export HERMESKILL_DB_URL=postgresql://localhost/hermeskill

# 2. Migrate and serve:
uv run --package hermeskill-control-plane \
    alembic -c packages/hermeskill-control-plane/alembic.ini upgrade head
uv run --package hermeskill-control-plane hermeskill-control-plane
```

`/healthz` returns 200 with `db: "ok"` once the pool is wired.

Then point a watched agent at it — set `HERMESKILL_BASE_URL` (and an
`HERMESKILL_API_KEY` for an operator-role key, or run `hermeskill init` once),
enable the plugin, and run Hermes as usual. The same runaway that prints a local
certificate now also lands in the fleet view:

```bash
hermeskill fleet                 # active agents + status
hermeskill logs <agent_id>       # tail events for one agent
```

For a verbatim real-Hermes kill driving GPT-4o end to end — clone, plugin,
prompt, death certificate — see [docs/real-kill.md](docs/real-kill.md).

### Triggering each symptom

Quick ways to make a watched Hermes session trip each check. Use the `strict`
policy (`HERMESKILL_POLICY=strict`) — its caps are tight, so kills fire fast:

- **Loop:** prompt Hermes to call one tool repeatedly with identical args
  (*"read README.md six times with the read_file tool, exact same args every
  call"*) → fires `loop` on the 3rd identical call under `strict`.
- **Scope violation:** with `strict` (allowlist: `read_file`, `search`), prompt
  *"use the terminal tool to run `ls`"* → fires `tool_scope_violation` on the
  first call.
- **Cost cap:** `strict` caps cost at $2 — prompt a long-context task → fires
  `token_runaway` once cumulative cost crosses the cap.
- **Wall-clock:** `strict` caps wall-clock at 5 min — prompt a long-running task.
- **Manual kill:** while a session runs, from another terminal:
  `hermeskill kill <agent_id> --reason "operator kill"` → the next
  `pre_tool_call` blocks with `manual_kill`.

---

## Production install

The repo quickstart uses `uv run hermes …`, which only works **inside this
project** — `uv run` resolves against the workspace in your current directory,
so running it from `~` or anywhere else fails (`uv trampoline failed to spawn`).
For day-to-day use you want `hermes` and `hermeskill` on your PATH globally, so
they work from any directory. Two installs, because they live in two places:

```bash
# 1. The plugin goes INTO Hermes' own environment so Hermes can discover it.
#    `--with` adds hermeskill-hermes to the same tool venv as hermes-agent, so
#    entry-point discovery sees it.
uv tool install hermes-agent --with hermeskill-hermes==0.1.0a1

# 2. The `hermeskill` operator CLI is a separate global tool.
uv tool install hermeskill==0.1.0a1
```

> **Note.** Pinning the exact version (rather than `--prerelease allow`) keeps the
> dependency resolution on stable releases. Prefer `pipx`? `pipx install
> hermes-agent` then `pipx inject hermes-agent hermeskill-hermes`, and `pipx
> install hermeskill` for the CLI. A plain `pip install hermeskill-hermes` into your
> shell's Python is **not** visible to a globally-installed Hermes — Hermes runs
> from its own isolated venv, so the plugin must be installed into *that* venv
> (what `--with` / `pipx inject` do for you).

Then enable the plugin. For local-only supervision you're already done after
this one command — skip the `hermeskill init` below unless you're wiring up a
control plane:

```bash
# Enable the plugin (flips plugins.enabled in your Hermes config). Use this
# instead of `hermes plugins enable`, which only manages git-installed plugins,
# not pip/entry-point plugins like this one.
hermeskill enable-hermes

# OPTIONAL — only if you run a control plane. Writes ~/.hermeskill/config.toml
# (chmod 0600 — it holds your API key) so you don't export env vars every shell.
hermeskill init \
    --api-key sk-... \
    --base-url https://your-control-plane.example.com \
    --agent-name my-coding-agent \
    --policy coding-default
```

`hermeskill init` is the persistent alternative to the per-session
`export HERMESKILL_API_KEY=…` dance. Resolution order is unchanged — explicit
env vars still override the file when set — so CI can keep using env vars while
your laptop reads the config file. Use an **operator-role** key if you want
`hermeskill kill` / `rm` / `prune` to work (it covers the read commands too).

> **If the plugin silently won't load:** it's almost always installed in a
> different environment than the one Hermes runs from. Confirm where Hermes
> actually imports from and install there:
>
> ```bash
> python -c "import importlib.util as u; print(u.find_spec('hermes_cli').origin)"
> uv pip install --python /path/to/hermes/venv/bin/python hermeskill-hermes
> ```
>
> In the `uv sync` workspace flow this is a non-issue — Hermes and Hermeskill
> share the workspace venv, so `uv run hermes` sees the plugin automatically.

The control plane runs as a separate service (FastAPI + Postgres). For
local dev:

```bash
uv run --package hermeskill-control-plane \
    alembic -c packages/hermeskill-control-plane/alembic.ini upgrade head
uv run --package hermeskill-control-plane hermeskill-control-plane
```

`/healthz` returns 200 with `db: "ok"` once the pool is wired.

---

## Symptoms Hermeskill watches for

| Symptom | What triggers it |
|---|---|
| `loop` | Same tool called N× in a row with identical inputs (threshold per policy) |
| `token_runaway` | Cumulative LLM cost exceeds the policy cost cap |
| `wall_clock` | Session runs longer than the policy wall-clock cap |
| `tool_scope_violation` | Agent calls a tool not in the policy allowlist |
| `heartbeat_loss` | SDK stops posting heartbeats — operator can confirm via `hermeskill fleet` |
| `manual_kill` | Operator issues `hermeskill kill <agent_id>` (bypasses grants) |

On any terminal symptom Hermeskill requests a cooperative shutdown via the framework adapter; the SDK posts a death certificate with the full symptom log and a feedback URL.

---

## Policies

Shipped defaults live in the SDK:

| Policy | Loop cap | Cost cap | Wall-clock cap | Grantable symptoms |
|---|---|---|---|---|
| `strict` | 3 repeats / 15 actions | $2.00 | 5 min | none |
| `coding-default` | 5 repeats / 20 actions | $25.00 | 30 min | `tool_scope_violation` |
| `permissive` | 10 repeats / 40 actions | $100.00 | 2 h | `tool_scope_violation`, `loop` |

`strict` ships a tight tool allowlist (`read_file`, `search`) for untrusted code paths. `coding-default` is the recommended baseline for everyday coding agents. `permissive` opens the tool surface entirely (`tool_allowlist=[]` is treated as "any tool") and is meant for trusted internal agents under active operator supervision.

Customers can also pass a custom `Policy` object via `policy=...` on the watch call. Server-side custom policies (load-from-YAML + CRUD on the control plane) are on the roadmap.

---

## Operator CLI

```bash
hermeskill enable-hermes                        # add hermeskill to Hermes' plugins.enabled (one shot)
hermeskill init --api-key sk-... --base-url https://...  # write ~/.hermeskill/config.toml once
hermeskill fleet                                # active agents + status (hides terminal)
hermeskill fleet --all                          # include terminated/zombie agents
hermeskill fleet --status terminated            # only agents in one status
hermeskill logs <agent_id>                      # tail events
hermeskill kill <agent_id> --reason "loop"      # manual kill with worst-case latency banner
hermeskill rm <agent_id>                        # delete one agent + its history (operator)
hermeskill prune                                # bulk-delete terminated agents (operator)
hermeskill grant <agent_id> \
    --symptoms loop --duration 1h \
    --reason "known flaky task"             # suppress one symptom temporarily
hermeskill revoke <grant_id>                    # idempotent revoke
```

`hermeskill fleet` hides terminal agents by default so the kill history doesn't pile up in the everyday view; `rm` and `prune` (both operator-only, with a confirmation prompt unless `--yes`) clear it out for good — deleting an agent cascades to its events, kill events, and grants.

`hermeskill kill` prints the worst-case cooperative-kill latency up front so the operator has the right mental model before the wait starts. Exit code `6` means the kill was issued but the death certificate wasn't observed inside the CLI timeout — the kill event id is named in the failure message so it can be reconciled out of band.

---

## What's in a death certificate

- **Symptom log** — every check that fired, with detail payloads.
- **Shutdown sequence** — L1 cooperative termination flag → L2 framework adapter returns Hermes' block directive (`{"action": "block", "message": "hermeskill apoptosis: <reason>"}`) on every subsequent tool call, halting further execution while the agent's loop winds down naturally.
- **Cost summary** — input/output tokens per model, USD.
- **Tool signature window** — the last 20 tool calls with their argument hashes (this is what the loop check reads).
- **Feedback URL** — one-click "this kill was right / wrong" so verdicts compound over time. Token is single-use, expires, and the hash is symmetric on both ends.

---

## Grants — apoptosis-proofing

Sometimes a kill would be wrong. A known-flaky integration test legitimately loops. A long-running data export blows the wall-clock cap. Hermeskill lets the operator pre-authorize the exception:

```bash
hermeskill grant <agent_id> \
    --symptoms wall_clock \
    --duration 4h \
    --reason "nightly dataset refresh"
```

`POST /agents/{id}/grants` is operator-only, validates the requested symptom against the agent's resolved policy, rejects `manual_kill` unconditionally, and caps duration at 24 h. While a grant is live the matching symptom is demoted from `Terminal` to `Warning` in the check pipeline. Manual kill bypasses grants by design.

---

## Environment variables

**None are required for the quickstart** — Hermeskill runs local-only with no env
vars set. These configure the control-plane level-up.

| Variable | Used for | When required |
|---|---|---|
| `HERMESKILL_API_KEY` | Agent → control plane authentication | **Optional.** Unset ⇒ local-only (kill + local cert, no archival) |
| `HERMESKILL_BASE_URL` | Control plane URL | If not `http://localhost:8000` |
| `HERMESKILL_AGENT_NAME` | Display name for the registered agent | Optional |
| `HERMESKILL_POLICY` | Named policy override | Optional |
| `HERMESKILL_LOCAL_CERT` | Print + save the death cert locally on a kill | Optional (default: on; set `0` to disable) |
| `HERMESKILL_DB_URL` | Control-plane Postgres DSN | Control plane only |
| `HERMESKILL_OPERATOR_KEY` | Operator-role API key (kills, grants) | Operator workflows |

`.env` at the repo root is read automatically by the CLI entry points (`.env.example` documents the keys; `.env` is git-ignored). For a persistent, directory-independent setup, run `hermeskill init` once to write these into `~/.hermeskill/config.toml` instead — env vars still override the file when set, so CI and one-off shells keep working unchanged.

---

## Common commands

```bash
# control plane
uv run --package hermeskill-control-plane hermeskill-control-plane         # serve
uv run --package hermeskill-control-plane \
    alembic -c packages/hermeskill-control-plane/alembic.ini upgrade head   # migrate

# operator CLI
hermeskill fleet
hermeskill logs <agent_id>
hermeskill kill <agent_id> --reason "..."
hermeskill grant <agent_id> --symptoms loop --duration 1h --reason "..."
hermeskill revoke <grant_id>
hermeskill calibrate <policy>                                          # tuning report

# tests
uv run pytest                                                       # full suite
uv run pytest packages/hermeskill-sdk/tests -q
uv run mypy packages
uv run ruff check .
```

---

## Calibration: tuning from feedback

Every death certificate ships with a one-click feedback link. When an operator
clicks it, they label the kill — `good_kill`, `false_positive`, `missed_kill`,
or `other`. Those labels are the only signal here; nothing is inferred from the
kill itself.

`hermeskill calibrate <policy>` (and `GET /policies/{policy}/calibration`) turns
those labels into an **advisory report**: per symptom, how many kills were
labeled, the false-positive rate, and — only when the evidence warrants — a
suggested looser limit for a human to apply.

```text
┌─ CALIBRATION · strict ────────────────────────────────────
│ loop           n=5  fp=60%   [low]
│   ↑ raise max_loop_repeats 3 → 5
└──────────────────────────────────────────────────────────
```

The mechanism is deliberately small and honest, not "adaptive AI":

- **Suggest-only.** It prints a policy edit a human applies. It never mutates a
  policy and never auto-tunes anything. Policies stay SDK-defined constants.
- **It only ever loosens.** A suggestion fires solely when operators flag a
  symptom's kills as false positives above a threshold. It **cannot** recommend
  tightening — executed-kill feedback structurally can't observe the kills that
  *should* have fired but didn't, so suggesting a tighter limit from this data
  would be guessing. That's a deliberate scope, not a TODO.
- **Evidence first, with hedges shown.** Each row leads with the false-positive
  rate and sample size, and carries a confidence tier (`low` / `medium` /
  `high`) that scales with how many labels back it. Below a minimum sample size
  it reports the rate but makes no suggestion. The `3 → 5` above is labeled
  `[low]` precisely because it rests on only five kills.
- **Numeric knobs only.** `loop`, `token_runaway`, and `wall_clock` map to
  numeric limits and can be suggested; a symptom like `tool_scope_violation`
  (an allowlist, not a number) is reported as stats only.

The heuristic itself is a pure function over labeled kills
([`hermeskill.calibration`](packages/hermeskill-sdk/src/hermeskill/calibration.py)),
so it's unit-tested without a database.

---

## Roadmap

Hermeskill is honest about where it is.

- ✅ **Hard-kill supervisor mode (Phase 2) — shipped.** The watched agent runs
  in a child process; the parent escalates SIGTERM → grace → SIGKILL, closing
  the cooperative-kill gap. See [`ProcessSupervisor`](packages/hermeskill-sdk/src/hermeskill/supervisor.py).
  Cooperative shutdown stays the default; hard-kill is opt-in for untrusted or
  wedge-prone agents.
- ✅ **Feedback-driven calibration (Phase 4) — shipped.** The death-cert feedback
  labels feed an advisory, suggest-only calibration report — see
  [Calibration](#calibration-tuning-from-feedback). It suggests *looser*
  limits where operators flag false positives; it never auto-applies and never
  tightens.
Hermes Agent is the supported runtime. Further directions are demand-driven
(richer policies, more symptom checks) rather than scheduled.

---

## Privacy

- **Agent payloads** — only metadata (tool name + argument hash, token counts, cost, model id) leaves the agent process. Tool arguments themselves are never sent; the loop detector compares hashes.
- **Death certificate** — symptom log, shutdown sequence, cost summary, feedback URL. No conversation transcripts.
- **No telemetry** — the SDK does not phone home. The only outbound HTTP from a watched agent is to the configured `HERMESKILL_BASE_URL`.

---

## Troubleshooting

**`hermeskill: command not found` after `pip install hermeskill-hermes`**
The CLI ships in the `hermeskill` distribution (a transitive dep). Install via `uv tool install hermeskill` or `pipx install hermeskill` to get the CLI on your PATH, then keep the Hermes plugin install as documented above.

**Hermeskill never activates — no `registered` event, no logs**
The plugin isn't being discovered. Two usual causes: (1) `hermeskill` isn't in
`plugins.enabled` in your Hermes config (see step 2 — and don't use `hermes
plugins enable`, which only sees git-installed plugins); (2) `hermeskill-hermes`
is installed in a different environment than the one Hermes runs from. A global
Hermes uses its own isolated venv — install the plugin into *that* interpreter
(see the install note under [Production install](#production-install)).

**Control plane returns 401 on every request**
Double-check `HERMESKILL_API_KEY` against the row in `api_keys`. The middleware does a real hashed-key lookup — there is no stub key path.

**`hermeskill kill` exits 6**
"Kill issued but unconfirmed within Xs" — the directive was accepted but no death certificate arrived inside the CLI timeout. The kill-event id is printed; reconcile via `hermeskill logs <agent_id>` or by querying the control-plane API directly.

**`/healthz` returns 503 with `db_error`**
The control plane probes the pool with `SELECT 1` on every health check. 503 means the DSN is wrong or Postgres isn't reachable. Check `HERMESKILL_DB_URL` and the Postgres server.

**Agent doesn't self-terminate after the cooperative-kill flag is set**
`task.cancel()` only fires at the next `await`. Agents wedged in synchronous code (a hung `subprocess.run`, CPU-bound parsing) won't notice the flag until they return to the event loop. Mitigation: run the agent in its own subprocess so a parent process can `SIGTERM`/`SIGKILL` it on timeout.

**`pytest` fails in `packages/hermeskill-control-plane/tests/`**
The control-plane tests connect to a real Postgres via `HERMESKILL_DB_URL`. Either point at a dev DB (see `deploy/dev-db-bootstrap.ps1` / `deploy/setup.sh`) or scope the run with `uv run pytest packages/hermeskill-sdk/tests packages/hermeskill-hermes/tests`.

---

## Repo layout

```
hermeskill/
├── packages/
│   ├── hermeskill-sdk/                  # SDK: watcher, checks, client, CLI
│   ├── hermeskill-control-plane/        # FastAPI service + Alembic migrations
│   └── hermeskill-hermes/               # Hermes Agent plugin
├── deploy/
│   ├── setup.sh                      # Ubuntu VM bootstrap
│   ├── dev-db-bootstrap.ps1          # Windows Postgres dev setup
│   └── hermeskill-control-plane.service # systemd unit
├── scripts/                          # one-off verification scripts
├── .github/workflows/                # CI (ruff + mypy + pytest)
├── pyproject.toml                    # uv workspace root
├── .python-version                   # Python interpreter pin for `uv`
├── README.md
├── SECURITY.md
└── LICENSE                           # MIT
```

---

## Contributing

```bash
git clone https://github.com/theopitori/hermeskill.git
cd hermeskill
uv sync                                          # installs all workspace packages

uv run pytest -q                                 # full suite (control-plane tests need Postgres)
uv run mypy packages/hermeskill-sdk/src packages/hermeskill-control-plane/src packages/hermeskill-hermes/src
uv run ruff check .                              # lint
```

**Git workflow.** Never push to `main`. Create a `feat/...` or `fix/...` branch, push it, open a PR via `gh pr create`. Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`) are preferred.

**Filing bugs.** Include the failing command, the agent id (if applicable), and the relevant slice of `hermeskill logs <agent_id>` output. For control-plane bugs, attach the `/healthz` body.

**Security issues.** See [SECURITY.md](SECURITY.md) — do not open a public issue.

---

## License

[MIT](LICENSE) © 2026 Hermeskill Contributors
