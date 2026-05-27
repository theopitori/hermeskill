# Caspase: Apoptosis Protocol for AI Agents

Drop one plugin into your agent runtime and Caspase watches every tool call and LLM turn. The moment it sees a runaway loop, a budget breach, a wall-clock overrun, or an out-of-scope tool call, it terminates the agent cleanly and writes a death certificate you can audit.

```bash
pip install caspase-hermes
```

That's it. Start your Hermes Agent session and Caspase activates automatically. Every session is queryable via the operator CLI (`caspase agents list`, `caspase logs <id>`) and the control-plane HTTP API; every kill is explainable.

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
| Python | 3.14+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| uv *(recommended)* | any | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Postgres *(control plane only)* | 14+ | `psql --version` | [postgresql.org](https://www.postgresql.org/download/) |

**Windows quick install:**
```powershell
winget install astral-sh.uv
```

**macOS quick install:**
```bash
brew install python@3.14 uv
```

**Ubuntu/Debian:**
```bash
sudo apt install python3.14 python3-pip
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Install

The agent-side install is one package. The control plane runs separately (locally for development, or as a service in production).

### Agent side (Hermes)

```bash
pip install caspase-hermes
```

Drop the plugin into Hermes:

```bash
python -c "
import caspase_hermes, pathlib, shutil
src = pathlib.Path(caspase_hermes.__file__).parent
dst = pathlib.Path.home() / '.hermes' / 'plugins' / 'caspase'
shutil.copytree(src, dst, dirs_exist_ok=True)
print('installed →', dst)
"
```

Configure via environment variables (or `~/.hermes/.env`):

```bash
export CASPASE_API_KEY=sk-...
export CASPASE_BASE_URL=https://your-control-plane.example.com
export CASPASE_AGENT_NAME=my-coding-agent     # optional display name
export CASPASE_POLICY=coding-default          # optional policy
```

Then run Hermes normally. Caspase activates automatically.

### Control plane (local development)

```bash
git clone https://github.com/seijeupessoal-ui/Caspase.git
cd Caspase
uv sync
uv run --package caspase-control-plane \
    alembic -c packages/caspase-control-plane/alembic.ini upgrade head
uv run --package caspase-control-plane caspase-control-plane
```

The service listens on `http://localhost:8000`. `/healthz` returns 200 with a `db: "ok"` field once the pool is wired.

---

## Symptoms Caspase watches for

| Symptom | What triggers it |
|---|---|
| `loop` | Same tool called N× in a row with identical inputs (threshold per policy) |
| `token_runaway` | Cumulative LLM cost exceeds the policy cost cap |
| `wall_clock` | Session runs longer than the policy wall-clock cap |
| `tool_scope_violation` | Agent calls a tool not in the policy allowlist |
| `heartbeat_loss` | SDK stops posting heartbeats — operator can confirm via `caspase agents list` |
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
caspase agents list                          # registered agents + status
caspase logs <agent_id>                      # tail events
caspase kill <agent_id> --reason "loop"      # manual kill with worst-case latency banner
caspase grants create <agent_id> \
    --symptom loop --duration 1h \
    --reason "known flaky task"             # suppress one symptom temporarily
caspase grants revoke <grant_id>             # idempotent revoke
```

`caspase kill` prints the worst-case cooperative-kill latency up front so the operator has the right mental model before the wait starts. Exit code `6` means the kill was issued but the death certificate wasn't observed inside the CLI timeout — the kill event id is named in the failure message so it can be reconciled out of band.

---

## What's in a death certificate

- **Symptom log** — every check that fired, with detail payloads.
- **Shutdown sequence** — L1 cooperative termination flag → L2 framework adapter (arms a `tool_override` kill stub at the next tool boundary, triggering controlled shutdown via the Hermes runtime).
- **Cost summary** — input/output tokens per model, USD.
- **Tool signature window** — the last 20 tool calls with their argument hashes (this is what the loop check reads).
- **Feedback URL** — one-click "this kill was right / wrong" so verdicts compound over time. Token is single-use, expires, and the hash is symmetric on both ends.

---

## Grants — apoptosis-proofing

Sometimes a kill would be wrong. A known-flaky integration test legitimately loops. A long-running data export blows the wall-clock cap. Caspase lets the operator pre-authorize the exception:

```bash
caspase grants create <agent_id> \
    --symptom wall_clock \
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
caspase agents list
caspase logs <agent_id>
caspase kill <agent_id> --reason "..."
caspase grants create <agent_id> --symptom loop --duration 1h --reason "..."
caspase grants revoke <grant_id>

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
├── .python-version                   # Python 3.14
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
