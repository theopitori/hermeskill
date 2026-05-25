# Caspase: Apoptosis Protocol for AI Agents

Drop one plugin into your agent runtime and Caspase watches every tool call and LLM turn. The moment it sees a runaway loop, a budget breach, a wall-clock overrun, or an out-of-scope tool call, it terminates the agent cleanly and writes a death certificate you can audit.

```bash
pip install stasis-hermes
```

That's it. Start your Hermes Agent session and Caspase activates automatically. Every session shows up in the dashboard; every kill is explainable.

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
| Python | 3.12+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| uv *(recommended)* | any | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Postgres *(control plane only)* | 14+ | `psql --version` | [postgresql.org](https://www.postgresql.org/download/) |

**Windows quick install:**
```powershell
winget install astral-sh.uv
```

**macOS quick install:**
```bash
brew install python@3.12 uv
```

**Ubuntu/Debian:**
```bash
sudo apt install python3.12 python3-pip
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Install

The agent-side install is one package. The control plane runs separately (locally for development, or as a service in production).

### Agent side (Hermes)

```bash
pip install stasis-hermes
```

Drop the plugin into Hermes:

```bash
python -c "
import stasis_hermes, pathlib, shutil
src = pathlib.Path(stasis_hermes.__file__).parent
dst = pathlib.Path.home() / '.hermes' / 'plugins' / 'caspase'
shutil.copytree(src, dst, dirs_exist_ok=True)
print('installed →', dst)
"
```

Configure via environment variables (or `~/.hermes/.env`):

```bash
export STASIS_API_KEY=sk-...
export STASIS_BASE_URL=https://your-control-plane.example.com
export STASIS_AGENT_NAME=my-coding-agent     # optional display name
export STASIS_POLICY=coding-default          # optional policy
```

Then run Hermes normally. Caspase activates automatically.

> The environment-variable prefix is `STASIS_*` for now — the in-flight package rename keeps the prefix until clients have migrated. A `CASPASE_*` alias is on the roadmap.

### Control plane (local development)

```bash
git clone https://github.com/seijeupessoal-ui/Stasis.git
cd Stasis
uv sync
uv run --package stasis-control-plane \
    alembic -c packages/stasis-control-plane/alembic.ini upgrade head
uv run --package stasis-control-plane stasis-control-plane
```

The service listens on `http://localhost:8000`. `/healthz` returns 200 with a `db: "ok"` field once the pool is wired.

---

## Symptoms Caspase watches for

| Symptom | What triggers it |
|---|---|
| `loop` | Same tool called 5× in a row with identical inputs (per policy) |
| `token_runaway` | Cumulative LLM cost exceeds the policy cost cap |
| `wall_clock` | Session runs longer than the policy wall-clock cap |
| `tool_scope_violation` | Agent calls a tool not in the policy allowlist |
| `heartbeat_loss` | SDK stops posting heartbeats — operator can confirm via the dashboard |
| `manual_kill` | Operator issues `stasis kill <agent_id>` (bypasses grants) |

On any terminal symptom Caspase requests a cooperative shutdown via the framework adapter; the SDK posts a death certificate with the full symptom log and a feedback URL.

---

## Policies

Shipped defaults live in the SDK:

| Policy | Loop cap | Cost cap | Wall-clock cap |
|---|---|---|---|
| `coding-default` | 5 repeats / 20 actions | $2.00 | 30 min |
| `coding-permissive` | 8 repeats / 40 actions | $10.00 | 2 h |

Customers can pass a custom policy via `policy=...` on the watch call, or — for fleet-wide overrides — define one in the control plane and reference it by name. Policy resolution happens server-side at agent registration.

---

## Operator CLI

```bash
stasis agents list                          # registered agents + status
stasis logs <agent_id>                      # tail events
stasis kill <agent_id> --reason "loop"      # manual kill with worst-case latency banner
stasis grants create <agent_id> \
    --symptom loop --duration 1h \
    --reason "known flaky task"             # suppress one symptom temporarily
stasis grants revoke <grant_id>             # idempotent revoke
```

`stasis kill` prints the worst-case cooperative-kill latency up front so the operator has the right mental model before the wait starts. Exit code `6` means the kill was issued but the death certificate wasn't observed inside the CLI timeout — the kill event id is named in the failure message so it can be reconciled out of band.

---

## What's in a death certificate

- **Symptom log** — every check that fired, with detail payloads.
- **Shutdown sequence** — L1 cooperative request → L2 framework adapter → L2.5 subprocess kill escalation (when configured) → L3 hard exit.
- **Cost summary** — input/output tokens per model, USD.
- **Tool signature window** — the last 20 tool calls with their argument hashes (this is what the loop check reads).
- **Feedback URL** — one-click "this kill was right / wrong" so verdicts compound over time. Token is single-use, expires, and the hash is symmetric on both ends.

---

## Grants — apoptosis-proofing

Sometimes a kill would be wrong. A known-flaky integration test legitimately loops. A long-running data export blows the wall-clock cap. Caspase lets the operator pre-authorize the exception:

```bash
stasis grants create <agent_id> \
    --symptom wall_clock \
    --duration 4h \
    --reason "nightly dataset refresh"
```

`POST /agents/{id}/grants` is operator-only, validates the requested symptom against the agent's resolved policy, rejects `manual_kill` unconditionally, and caps duration at 24 h. While a grant is live the matching symptom is demoted from `Terminal` to `Warning` in the check pipeline. Manual kill bypasses grants by design.

---

## Environment variables

| Variable | Used for | When required |
|---|---|---|
| `STASIS_API_KEY` | Agent → control plane authentication | Always |
| `STASIS_BASE_URL` | Control plane URL | If not `http://localhost:8000` |
| `STASIS_AGENT_NAME` | Display name in the dashboard | Optional |
| `STASIS_POLICY` | Named policy override | Optional |
| `STASIS_DB_URL` | Control-plane Postgres DSN | Control plane only |
| `STASIS_DEV_KEY` | Disabled — auth uses the real `api_keys` table from M1 onward | Never |

`.env` at the repo root is read automatically by the demo and CLI entry points (`.env.example` documents the keys; `.env` is git-ignored).

---

## Common commands

```bash
# control plane
uv run --package stasis-control-plane stasis-control-plane         # serve
uv run --package stasis-control-plane \
    alembic -c packages/stasis-control-plane/alembic.ini upgrade head   # migrate

# demo (end-to-end DoD walkthrough)
uv run python demo/run_dod.py
uv run python demo/run_dod.py --skip-step 2,3

# operator CLI
stasis agents list
stasis logs <agent_id>
stasis kill <agent_id> --reason "..."
stasis grants create <agent_id> --symptom loop --duration 1h --reason "..."
stasis grants revoke <grant_id>

# tests
uv run pytest                                                       # full suite
uv run pytest packages/stasis-sdk/tests -q
uv run mypy packages
uv run ruff check .
```

---

## Privacy

- **Agent payloads** — only metadata (tool name + argument hash, token counts, cost, model id) leaves the agent process. Tool arguments themselves are never sent; the loop detector compares hashes.
- **Death certificate** — symptom log, shutdown sequence, cost summary, feedback URL. No conversation transcripts.
- **No telemetry** — the SDK does not phone home. The only outbound HTTP from a watched agent is to the configured `STASIS_BASE_URL`.

---

## Troubleshooting

**`stasis: command not found` after `pip install stasis-hermes`**
The CLI ships in the `stasis-agent` distribution (a transitive dep). Install via `uv tool install stasis-agent` or `pipx install stasis-agent` to get the CLI on your PATH, then keep the Hermes plugin install as documented above.

**Control plane returns 401 on every request**
Double-check `STASIS_API_KEY` against the row in `api_keys`. The middleware does a real hashed-key lookup from M1 onward — there is no stub key path.

**`stasis kill` exits 6**
"Kill issued but unconfirmed within Xs" — the directive was accepted but no death certificate arrived inside the CLI timeout. The kill-event id is printed; reconcile via `stasis logs <agent_id>` or the dashboard.

**`/healthz` returns 503 with `db_error`**
The control plane probes the pool with `SELECT 1` on every health check. 503 means the DSN is wrong or Postgres isn't reachable. Check `STASIS_DB_URL` and the Postgres server.

**Demo agent doesn't self-terminate after `--induce loop`**
The cooperative-kill path is best-effort if the agent is wedged in synchronous code. Run the demo agent in its own subprocess (as `demo/run_dod.py` does) so the L2.5 subprocess kill escalation can fire.

---

## Repo layout

```
Stasis/                                          # repo root (folder name still pre-rename)
├── packages/
│   ├── stasis-sdk/                              # SDK: watcher, checks, client, CLI
│   ├── stasis-control-plane/                    # FastAPI service + Alembic migrations
│   └── stasis-hermes/                           # Hermes Agent plugin
├── demo/
│   └── run_dod.py                               # 9-step end-to-end DoD walkthrough
├── deploy/
│   ├── setup.sh                                 # Ubuntu VM bootstrap
│   ├── dev-db-bootstrap.ps1                     # Windows Postgres dev setup
│   └── stasis-control-plane.service             # systemd unit
├── scripts/                                     # one-off verification scripts
├── pyproject.toml                               # uv workspace root
└── LICENSE                                      # MIT
```

> The Python package names (`stasis_agent`, `stasis_control_plane`, `stasis_hermes`) and the PyPI distributions (`stasis-agent`, `stasis-control-plane`, `stasis-hermes`) still carry the old `stasis-*` prefix while the code-rename PR is in flight. The product is **Caspase**; treat the package names as transitional.

---

## Contributing

```bash
git clone https://github.com/seijeupessoal-ui/Stasis.git
cd Stasis
uv sync                                          # installs all workspace packages

uv run pytest -q                                 # full suite
uv run mypy packages                             # strict type-check
uv run ruff check .                              # lint
```

**Git workflow.** Never push to `main`. Create a `feat/...` or `fix/...` branch, push it, open a PR via `gh pr create`. Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`) are preferred.

**Filing bugs.** Include the failing command, the agent id (if applicable), and the relevant slice of `stasis logs <agent_id>` output. For control-plane bugs, attach the `/healthz` body.

**Security issues.** See [SECURITY.md](SECURITY.md) — do not open a public issue.

---

## License

[MIT](LICENSE) © 2026 Caspase Contributors
