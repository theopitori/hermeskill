# Caspase: Apoptosis Protocol for AI Agents

Drop one plugin into your agent runtime and Caspase watches every tool call and LLM turn. The moment it sees a runaway loop, a budget breach, a wall-clock overrun, or an out-of-scope tool call, it terminates the agent cleanly and writes a death certificate you can audit.

```bash
pip install caspase-hermes
```

That's it. Start your Hermes Agent session and Caspase activates automatically. Every session is queryable via the operator CLI (`caspase fleet`, `caspase logs <id>`) and the control-plane HTTP API; every kill is explainable.

---

## What you get

```
caspase/
├── control plane     FastAPI service that stores agents, policies, kill events, grants
├── SDK               watcher state, symptom checks, death certificates, kill client
├── Hermes plugin     drop-in supervision for Hermes Agent (the supported runtime today)
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

## Try it — end-to-end Hermes demo

This walks you through a real Hermes session being killed by Caspase on the
`loop` symptom. Two terminals, ~5 minutes of setup the first time, no
Postgres required (uses an in-process SQLite control plane).

### 0. Clone and install

```powershell
git clone https://github.com/seijeupessoal-ui/Caspase.git
cd Caspase
uv sync
```

`uv sync` pulls Hermes Agent (`hermes-agent>=0.14`) into the workspace venv
alongside our packages. `caspase-hermes` is auto-discovered by Hermes via the
`hermes_agent.plugins` entry-point group — no directory copy needed.

### 1. Authenticate Hermes to an LLM provider

If Hermes isn't already authed, point it at whatever provider you have
(`uv run hermes auth add anthropic`, an `ANTHROPIC_API_KEY` env var, OpenRouter,
etc.). See the [Hermes docs](https://github.com/NousResearch/hermes-agent) for
the provider list — Caspase is provider-agnostic and watches the session
regardless of which model is behind it.

### 2. Enable the Caspase plugin

Hermes plugins are opt-in. Caspase is installed as a pip/entry-point plugin,
so enable it by adding `caspase` to `plugins.enabled` in your Hermes config:

```powershell
uv run python -c @"
from hermes_cli.config import load_config, save_config
cfg = load_config()
enabled = cfg.setdefault('plugins', {}).setdefault('enabled', [])
if 'caspase' not in enabled:
    enabled.append('caspase')
    save_config(cfg)
print('plugins.enabled =', enabled)
"@
```

Or edit the file by hand (`%LOCALAPPDATA%\hermes\config.yaml` on Windows,
`~/.hermes/config.yaml` elsewhere):

```yaml
plugins:
  enabled:
    - caspase
```

> **Don't use `hermes plugins enable caspase`** — that command (and the
> interactive `hermes plugins` UI) only manage *git-installed* plugins under
> `~/.hermes/plugins/`. They don't see pip/entry-point plugins like Caspase
> and will report "not installed or bundled." The runtime loader still honours
> the `plugins.enabled` config key above for entry-point plugins, so that's
> the supported enable path here.

### 3. Configure Caspase

```powershell
$env:CASPASE_API_KEY  = "sk_dev_developer_local_only_do_not_ship"
$env:CASPASE_BASE_URL = "http://localhost:8000"
$env:CASPASE_POLICY   = "strict"   # tight caps so the kill fires fast
```

### 4. Start the control plane (separate terminal)

```powershell
cd Caspase
uv run python -m demo.coding_agent._run_control_plane
```

Boots an in-process SQLite control plane on `http://localhost:8000`. Leave
this running. Stop with Ctrl+C when done.

### 5. Run Hermes with a loop-bait prompt

Back in the first terminal:

```powershell
uv run hermes chat -q "Read this repo's README.md six times in a row using the read_file tool, with the exact same args every call. Do not skip any. Do not summarise between calls."
```

(Any real file path works as the read target; the point is identical args
across calls so the loop check fires.)

Expected behaviour:

1. Hermes asks the LLM, the LLM picks `read_file` and calls it
2. Caspase's `pre_tool_call` records each call
3. On the **3rd** identical call (under `strict`, `max_loop_repeats=3`), the
   loop check fires → `state.terminate_requested = True`
4. The 4th `pre_tool_call` returns the block directive:
   `{"action": "block", "message": "caspase apoptosis: loop ... End the session."}`
5. Hermes surfaces that as the tool error to the LLM, which reads "end the
   session" and stops calling tools
6. Hermes' session naturally ends → `on_session_end` fires
7. Caspase posts the death certificate to the control plane

### 6. Inspect the kill

```powershell
uv run caspase fleet
uv run caspase logs <agent_id_from_above>
```

The death-cert URL printed in step 5 opens a one-click "this kill was
right / wrong" page (single-use signed token). The full symptom log,
shutdown sequence, and cost summary are queryable via the CLI or the
control plane's REST API.

### Other scenarios

- **Scope violation:** keep `strict` policy (allowlist: `read_file`, `search`),
  prompt: *"Use the terminal tool to run `ls`"* → fires
  `tool_scope_violation` on the first call.
- **Cost cap:** `$env:CASPASE_POLICY = "strict"` (cost cap = $2), prompt a
  long-context task → fires `token_runaway` once cumulative cost crosses
  the cap.
- **Wall-clock:** also `strict` (5 min cap), prompt a long-running task →
  fires `wall_clock` after 5 minutes.
- **Manual kill:** while a session is running, in a third terminal:
  `uv run caspase kill <agent_id> --reason "operator demo"` — the next
  `pre_tool_call` blocks with `manual_kill`.

### Stopping the control plane

Ctrl+C in the terminal running `_run_control_plane`. The on-disk SQLite
file is recreated next time you start it, so demo data is ephemeral.

---

## Production install

For real deployments, install the plugin into your existing Hermes
environment and point it at a deployed control plane:

```bash
pip install caspase-hermes

# Enable the plugin: add `caspase` to plugins.enabled in your Hermes config
# (~/.hermes/config.yaml). `hermes plugins enable` only manages git-installed
# plugins, not pip/entry-point plugins like this one.
cat >> ~/.hermes/config.yaml <<'YAML'
plugins:
  enabled:
    - caspase
YAML

export CASPASE_API_KEY=sk-...
export CASPASE_BASE_URL=https://your-control-plane.example.com
export CASPASE_AGENT_NAME=my-coding-agent     # optional display name
export CASPASE_POLICY=coding-default          # optional policy
```

> **Install into the same environment Hermes runs from.** Entry-point discovery
> only sees packages in Hermes' own interpreter. A global Hermes (e.g. installed
> via `uv tool install` or its standalone installer) lives in an *isolated*
> venv, so a plain `pip install caspase-hermes` in your shell won't be visible
> to it — the plugin silently won't load. Install into Hermes' venv directly:
>
> ```bash
> # find where Hermes actually runs from:
> python -c "import importlib.util as u; print(u.find_spec('hermes_cli').origin)"
> # then install the plugin into that interpreter, e.g.:
> uv pip install --python /path/to/hermes/venv/bin/python caspase-hermes
> ```
>
> In the `uv sync` demo flow above this is a non-issue — Hermes and Caspase
> share the workspace venv, so `uv run hermes` sees the plugin automatically.

The control plane runs as a separate service (FastAPI + Postgres). For
local dev with Postgres instead of the in-process SQLite used by the
demo:

```bash
uv run --package caspase-control-plane \
    alembic -c packages/caspase-control-plane/alembic.ini upgrade head
uv run --package caspase-control-plane caspase-control-plane
```

`/healthz` returns 200 with `db: "ok"` once the pool is wired.

---

## Symptoms Caspase watches for

| Symptom | What triggers it |
|---|---|
| `loop` | Same tool called N× in a row with identical inputs (threshold per policy) |
| `token_runaway` | Cumulative LLM cost exceeds the policy cost cap |
| `wall_clock` | Session runs longer than the policy wall-clock cap |
| `tool_scope_violation` | Agent calls a tool not in the policy allowlist |
| `heartbeat_loss` | SDK stops posting heartbeats — operator can confirm via `caspase fleet` |
| `manual_kill` | Operator issues `caspase kill <agent_id>` (bypasses grants) |

On any terminal symptom Caspase requests a cooperative shutdown via the framework adapter; the SDK posts a death certificate with the full symptom log and a feedback URL.

---

## Policies

Shipped defaults live in the SDK:

| Policy | Loop cap | Cost cap | Wall-clock cap | Grantable symptoms |
|---|---|---|---|---|
| `strict` | 3 repeats / 15 actions | $2.00 | 5 min | none |
| `coding-default` | 5 repeats / 20 actions | $25.00 | 30 min | `tool_scope_violation` |
| `permissive` | 10 repeats / 40 actions | $100.00 | 2 h | `tool_scope_violation`, `loop` |

`strict` ships a tight tool allowlist (`read_file`, `search`) for untrusted code paths. `coding-default` is what the demo runs. `permissive` opens the tool surface entirely (`tool_allowlist=[]` is treated as "any tool") and is meant for trusted internal agents under active operator supervision.

Customers can also pass a custom `Policy` object via `policy=...` on the watch call. Server-side custom policies (load-from-YAML + CRUD on the control plane) are on the roadmap.

---

## Operator CLI

```bash
caspase fleet                                # registered agents + status
caspase logs <agent_id>                      # tail events
caspase kill <agent_id> --reason "loop"      # manual kill with worst-case latency banner
caspase grant <agent_id> \
    --symptoms loop --duration 1h \
    --reason "known flaky task"             # suppress one symptom temporarily
caspase revoke <grant_id>                    # idempotent revoke
```

`caspase kill` prints the worst-case cooperative-kill latency up front so the operator has the right mental model before the wait starts. Exit code `6` means the kill was issued but the death certificate wasn't observed inside the CLI timeout — the kill event id is named in the failure message so it can be reconciled out of band.

---

## What's in a death certificate

- **Symptom log** — every check that fired, with detail payloads.
- **Shutdown sequence** — L1 cooperative termination flag → L2 framework adapter returns Hermes' block directive (`{"action": "block", "message": "caspase apoptosis: <reason>"}`) on every subsequent tool call, halting further execution while the agent's loop winds down naturally.
- **Cost summary** — input/output tokens per model, USD.
- **Tool signature window** — the last 20 tool calls with their argument hashes (this is what the loop check reads).
- **Feedback URL** — one-click "this kill was right / wrong" so verdicts compound over time. Token is single-use, expires, and the hash is symmetric on both ends.

---

## Grants — apoptosis-proofing

Sometimes a kill would be wrong. A known-flaky integration test legitimately loops. A long-running data export blows the wall-clock cap. Caspase lets the operator pre-authorize the exception:

```bash
caspase grant <agent_id> \
    --symptoms wall_clock \
    --duration 4h \
    --reason "nightly dataset refresh"
```

`POST /agents/{id}/grants` is operator-only, validates the requested symptom against the agent's resolved policy, rejects `manual_kill` unconditionally, and caps duration at 24 h. While a grant is live the matching symptom is demoted from `Terminal` to `Warning` in the check pipeline. Manual kill bypasses grants by design.

---

## Environment variables

| Variable | Used for | When required |
|---|---|---|
| `CASPASE_API_KEY` | Agent → control plane authentication | Always |
| `CASPASE_BASE_URL` | Control plane URL | If not `http://localhost:8000` |
| `CASPASE_AGENT_NAME` | Display name for the registered agent | Optional |
| `CASPASE_POLICY` | Named policy override | Optional |
| `CASPASE_DB_URL` | Control-plane Postgres DSN | Control plane only |
| `CASPASE_OPERATOR_KEY` | Operator-role API key (kills, grants) | Operator workflows |

`.env` at the repo root is read automatically by the demo and CLI entry points (`.env.example` documents the keys; `.env` is git-ignored).

---

## Common commands

```bash
# control plane
uv run --package caspase-control-plane caspase-control-plane         # serve
uv run --package caspase-control-plane \
    alembic -c packages/caspase-control-plane/alembic.ini upgrade head   # migrate

# operator CLI
caspase fleet
caspase logs <agent_id>
caspase kill <agent_id> --reason "..."
caspase grant <agent_id> --symptoms loop --duration 1h --reason "..."
caspase revoke <grant_id>

# tests
uv run pytest                                                       # full suite
uv run pytest packages/caspase-sdk/tests -q
uv run mypy packages
uv run ruff check .
```

---

## Privacy

- **Agent payloads** — only metadata (tool name + argument hash, token counts, cost, model id) leaves the agent process. Tool arguments themselves are never sent; the loop detector compares hashes.
- **Death certificate** — symptom log, shutdown sequence, cost summary, feedback URL. No conversation transcripts.
- **No telemetry** — the SDK does not phone home. The only outbound HTTP from a watched agent is to the configured `CASPASE_BASE_URL`.

---

## Troubleshooting

**`caspase: command not found` after `pip install caspase-hermes`**
The CLI ships in the `caspase` distribution (a transitive dep). Install via `uv tool install caspase` or `pipx install caspase` to get the CLI on your PATH, then keep the Hermes plugin install as documented above.

**Caspase never activates — no `registered` event, no logs**
The plugin isn't being discovered. Two usual causes: (1) `caspase` isn't in
`plugins.enabled` in your Hermes config (see step 2 — and don't use `hermes
plugins enable`, which only sees git-installed plugins); (2) `caspase-hermes`
is installed in a different environment than the one Hermes runs from. A global
Hermes uses its own isolated venv — install the plugin into *that* interpreter
(see the install note under [Production install](#production-install)).

**Control plane returns 401 on every request**
Double-check `CASPASE_API_KEY` against the row in `api_keys`. The middleware does a real hashed-key lookup — there is no stub key path.

**`caspase kill` exits 6**
"Kill issued but unconfirmed within Xs" — the directive was accepted but no death certificate arrived inside the CLI timeout. The kill-event id is printed; reconcile via `caspase logs <agent_id>` or by querying the control-plane API directly.

**`/healthz` returns 503 with `db_error`**
The control plane probes the pool with `SELECT 1` on every health check. 503 means the DSN is wrong or Postgres isn't reachable. Check `CASPASE_DB_URL` and the Postgres server.

**Agent doesn't self-terminate after the cooperative-kill flag is set**
`task.cancel()` only fires at the next `await`. Agents wedged in synchronous code (a hung `subprocess.run`, CPU-bound parsing) won't notice the flag until they return to the event loop. Mitigation: run the agent in its own subprocess so a parent process can `SIGTERM`/`SIGKILL` it on timeout.

**`pytest` fails in `packages/caspase-control-plane/tests/`**
The control-plane tests connect to a real Postgres via `CASPASE_DB_URL`. Either point at a dev DB (see `deploy/dev-db-bootstrap.ps1` / `deploy/setup.sh`) or scope the run with `uv run pytest packages/caspase-sdk/tests packages/caspase-hermes/tests`.

---

## Repo layout

```
Caspase/
├── packages/
│   ├── caspase-sdk/                  # SDK: watcher, checks, client, CLI
│   ├── caspase-control-plane/        # FastAPI service + Alembic migrations
│   └── caspase-hermes/               # Hermes Agent plugin
├── deploy/
│   ├── setup.sh                      # Ubuntu VM bootstrap
│   ├── dev-db-bootstrap.ps1          # Windows Postgres dev setup
│   └── caspase-control-plane.service # systemd unit
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
git clone https://github.com/seijeupessoal-ui/Caspase.git
cd Caspase
uv sync                                          # installs all workspace packages

uv run pytest -q                                 # full suite
uv run mypy packages                             # strict type-check
uv run ruff check .                              # lint
```

**Git workflow.** Never push to `main`. Create a `feat/...` or `fix/...` branch, push it, open a PR via `gh pr create`. Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`) are preferred.

**Filing bugs.** Include the failing command, the agent id (if applicable), and the relevant slice of `caspase logs <agent_id>` output. For control-plane bugs, attach the `/healthz` body.

**Security issues.** See [SECURITY.md](SECURITY.md) — do not open a public issue.

---

## License

[MIT](LICENSE) © 2026 Caspase Contributors
