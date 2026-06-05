"""Pytest fixtures for control-plane integration tests.

Tests run against the same dev Postgres the developer uses (HERMESKILL_DB_URL
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

# Make the tests directory importable so test files can `from _keys import ...`.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# Make the top-level `demo/` package importable from tests.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


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


from _keys import DEV_DEVELOPER_KEY, DEV_HEADERS, DEV_OPERATOR_KEY, OP_HEADERS  # noqa: E402

__all__ = ["DEV_DEVELOPER_KEY", "DEV_HEADERS", "DEV_OPERATOR_KEY", "OP_HEADERS"]
