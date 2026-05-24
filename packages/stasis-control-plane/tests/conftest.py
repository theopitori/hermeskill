"""Pytest fixtures for control-plane integration tests.

Tests run against the same dev Postgres the developer uses (STASIS_DB_URL
from `.env` or shell). The migration must have been applied beforehand —
the per-test fixtures only clean up rows created by the test, they don't
manage schema.

Schema isolation (separate test DB, parallel-test safety, rollback fixtures)
can wait until we have enough tests to justify the complexity.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# Make the top-level `demo/` package importable from tests.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Make `from conftest import ...` work in `--import-mode=importlib`.
# Without this, sibling test files can't pull in the shared dev-key
# constants below.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from control_plane.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def cleanup_agents() -> AsyncIterator[list[str]]:
    """Yields a list that tests append agent_ids to; on teardown deletes them."""
    from control_plane.db.session import SessionLocal

    created: list[str] = []
    yield created
    if created:
        async with SessionLocal() as session:
            await session.execute(
                text("DELETE FROM agents WHERE id::text = ANY(:ids)"),
                {"ids": created},
            )
            await session.commit()


# Dev keys mirror the migration seed
# (packages/stasis-control-plane/migrations/versions/0001_initial_schema.py).
# Test files import these instead of redefining them per-file.
DEV_DEVELOPER_KEY = "sk_dev_developer_local_only_do_not_ship"
DEV_OPERATOR_KEY = "sk_dev_operator_local_only_do_not_ship"
DEV_HEADERS = {"Authorization": f"Bearer {DEV_DEVELOPER_KEY}"}
OP_HEADERS = {"Authorization": f"Bearer {DEV_OPERATOR_KEY}"}
