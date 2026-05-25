# caspase-control-plane

The Caspase control plane — FastAPI service that stores agent registrations,
policies, kill events, death certificates, feedback labels, and
apoptosis-proofing grants. Backed by Postgres via SQLAlchemy 2.0 + Alembic.

Run locally for development:

```bash
uv run --package caspase-control-plane \
    alembic -c packages/caspase-control-plane/alembic.ini upgrade head
uv run --package caspase-control-plane caspase-control-plane
```

`/healthz` exercises the pool with `SELECT 1` and returns 503 with a
`db_error` field on failure.

See the [repo root README](../../README.md) for the full picture — install,
environment variables, deployment, operator CLI.
