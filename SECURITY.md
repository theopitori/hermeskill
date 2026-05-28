# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes (alpha) |
| < 0.1   | No |

The 0.1.x line is the active pre-1.0 alpha. Once 1.0 ships, the previous minor will be supported for security fixes for 6 months.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues via [GitHub's private vulnerability reporting](https://github.com/theopidori/caspase/security/advisories/new), or email the maintainers directly. Please include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact (data exposure, privilege escalation, denial of service, …)
- Suggested fix, if any

We acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Security Model

Caspase has two components with different threat profiles:

- **SDK + Hermes plugin** — runs **inside the agent process** on customer infrastructure. It posts metadata to the control plane; it never reads or transmits tool arguments or LLM transcripts.
- **Control plane** — a FastAPI service the customer runs (locally, in their own VPC, or on managed infrastructure). It stores agent registrations, policies, kill events, grants, and feedback labels in Postgres.

The SDK ↔ control-plane channel is authenticated with a per-customer API key bound to a real `api_keys` row (hashed; no plaintext at rest, no static dev-key path in production).

### Threat Surface

| Vector | Mitigation |
|--------|-----------|
| Authn replay | Every request requires `CASPASE_API_KEY`. Middleware performs a hashed-key DB lookup on every request — no stub key, no environment-only bypass. Dev keys are seeded by the first migration and live only in gitignored `.env` files. |
| Feedback-URL forgery | `control_plane.feedback_tokens.hash_feedback_token` is the single SHA-256 helper, used symmetrically at issue and lookup. Only the hash is persisted; the raw token lives only inside the death certificate's `feedback_url`. Tokens are single-use (`used_at` → 410) and expiring (`expires_at` → 404). |
| Privilege escalation via grants | `POST /agents/{id}/grants` is operator-role only, validates the requested symptom against the agent's resolved policy, rejects `manual_kill` unconditionally, and caps duration at 24 h. Manual kills bypass grants by design (operator-issued kill cannot be masked). |
| Tool-argument leakage | The SDK never transmits tool arguments. The loop detector compares **hashes** of argument tuples (`tool_signatures` deque, max length 20). Cost and token counts are aggregated server-side per model — no transcript ever crosses the wire. |
| Tool-scope evasion | The scope check is evaluated **before** the tool runs, not after. A tool not in the policy allowlist returns `Terminal(symptom=TOOL_SCOPE_VIOLATION)` and the framework adapter prevents the call. |
| Cooperative-kill bypass | `task.cancel()` only fires at the next `await`. Agents wedged in sync code (`subprocess.run` that hangs, CPU-bound parsing) won't notice the kill flag until they return to the event loop. Mitigation today: run the agent in its own subprocess so a parent can `SIGTERM`/`SIGKILL` it on timeout. A first-class subprocess-kill escalation inside the SDK is on the roadmap; in-process agents are documented as cooperative-cancellation-only. |
| Pricing-table rot | `pricing.py` carries a `last_updated` date per entry and warns at watcher init if any entry is more than 30 days old. Unknown models log `pricing_unknown` and skip the cost check rather than crashing the watcher — loop, wall-clock, and heartbeat checks still run. |
| SQL injection | All DB access is SQLAlchemy 2.0 with parameterised queries via the async engine. No raw string concatenation into SQL. Migrations are Alembic-managed and reviewed. |
| DB connectivity surprises | `/healthz` exercises the pool with `SELECT 1` on every probe; returns 503 with a `db_error` field on failure. CI exercises the 200 path against a live Postgres service container. |
| Windows event-loop drift | The SDK and control plane use `asyncpg`, which runs on `ProactorEventLoop` — no `SelectorEventLoop` workaround. The deprecated `set_event_loop_policy` shim was removed. |
| Migration / lock-step skew | Alembic head is the single source of truth; `alembic upgrade head` runs at deploy time. Integration tests run against a dedicated database (set via `CASPASE_DB_URL`) so they cannot accidentally exercise the dev DB. |

### What Caspase does NOT do

- **Does not exfiltrate tool arguments or LLM responses.** Only metadata (hash of args, token counts, cost, model id) crosses the wire.
- **Does not store API keys in plaintext.** `api_keys.key_hash` is the only persisted form.
- **Does not phone home.** The SDK's only outbound HTTP is to the customer-configured `CASPASE_BASE_URL`.
- **Does not use `shell=True`** in any subprocess call.
- **Does not eval, exec, or import-from-string** any data received from the network. Death certificates and grants are validated through Pydantic models at the boundary.

### Operator Responsibilities

- Treat `CASPASE_API_KEY` as a secret. Do not commit it; rotate it on staff churn.
- Run the control plane behind your own TLS terminator (reverse proxy, load balancer). The service speaks plain HTTP and is designed to live behind one.
- Restrict network access to the Postgres instance. The control plane is the only intended client.
- For untrusted or long-running agents, launch them in their own subprocess so a parent process can `SIGTERM`/`SIGKILL` on cooperative-kill timeout.
