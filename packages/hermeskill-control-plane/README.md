# hermeskill-control-plane

The Hermeskill control plane — FastAPI service that stores agent registrations,
policies, kill events, death certificates, feedback labels, and
apoptosis-proofing grants. Backed by Postgres via SQLAlchemy 2.0 + Alembic.

Run locally for development:

```bash
uv run --package hermeskill-control-plane \
    alembic -c packages/hermeskill-control-plane/alembic.ini upgrade head
uv run --package hermeskill-control-plane hermeskill-control-plane
```

`/healthz` exercises the pool with `SELECT 1` and returns 503 with a
`db_error` field on failure.

See the [repo root README](../../README.md) for the full picture — install,
environment variables, deployment, operator CLI.
