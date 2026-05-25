# stasis-control-plane

The Caspase control plane — FastAPI service that stores agent registrations,
policies, kill events, death certificates, feedback labels, and
apoptosis-proofing grants. Backed by Postgres via SQLAlchemy 2.0 + Alembic.

Run locally for development:

```bash
uv run --package stasis-control-plane \
    alembic -c packages/stasis-control-plane/alembic.ini upgrade head
uv run --package stasis-control-plane stasis-control-plane
```

`/healthz` exercises the pool with `SELECT 1` and returns 503 with a
`db_error` field on failure.

> The product is **Caspase**. The PyPI distribution and import path are still
> `stasis-control-plane` / `control_plane` while the code-rename PR is in
> flight.

See the [repo root README](../../README.md) for the full picture — install,
environment variables, deployment, operator CLI.
