"""Alembic environment using the async engine.

Reads the DB URL from `control_plane.settings`. Runs migrations online via
`engine.begin()` so we share the same connection config as the app.
"""

from __future__ import annotations

import asyncio

from alembic import context
from control_plane.db.models import Base
from control_plane.db.session import engine
from sqlalchemy.engine import Connection

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
