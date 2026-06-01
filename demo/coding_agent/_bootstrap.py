"""Boot an in-process control plane backed by SQLite for the demo.

Sets HERMESKILL_DB_URL *before* any control_plane import so the SQLAlchemy
engine uses SQLite + aiosqlite instead of Postgres. Creates the schema
via Base.metadata.create_all() (skipping Alembic to avoid the
postgresql_where= partial-index issue in migration 0004). Seeds the dev
API key rows, then starts uvicorn on localhost:8000 so any death-cert URL
printed by a demo agent is reachable from a browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
import uvicorn

_DEV_DEVELOPER_KEY = "sk_dev_developer_local_only_do_not_ship"
_DEV_OPERATOR_KEY = "sk_dev_operator_local_only_do_not_ship"
_CUSTOMER_ID = "11111111-1111-4111-8111-111111111111"
_API_KEY_ID = "22222222-2222-4222-8222-222222222222"
_API_KEY_NAME = "Demo Dev Key"
_OPERATOR_KEY_ID = "33333333-3333-4333-8333-333333333333"
_OPERATOR_KEY_NAME = "Demo Operator Key"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def start_control_plane() -> tuple[uvicorn.Server, asyncio.Task[Any]]:
    """Start the in-process control plane; return (server, task) for cleanup.

    Call this ONCE before setting HERMESKILL_API_KEY / HERMESKILL_BASE_URL so
    those env vars are ready when the SDK's HermeskillClient is constructed.
    """
    db_path = Path(tempfile.gettempdir()) / "hermeskill-demo.db"
    # Must be set before any control_plane.* import so Settings() picks it up.
    os.environ.setdefault("HERMESKILL_DB_URL", f"sqlite+aiosqlite:///{db_path}")

    # Lazy-import after the env var is in place.
    from sqlalchemy import JSON, text
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    # Delete any stale demo db so schema DDL patches below take full effect.
    if db_path.exists():
        db_path.unlink()

    # SQLite type affinity patches — must run BEFORE Base.metadata.create_all().
    #
    # 1) JSONB: models declare explicit mapped_column(JSONB, ...) which bypasses
    #    the type_annotation_map with_variant in base.py. Teach the compiler.
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.process(  # type: ignore[attr-defined]
            JSON(), **kw
        )

    # 2) UUID: PostgreSQL UUID with __visit_name__="UUID" would generate "UUID"
    #    DDL on SQLite. SQLite assigns NUMERIC affinity to unrecognized type names,
    #    so hex UUID strings get coerced to floats at storage time.  Render as
    #    VARCHAR(36) instead → TEXT affinity → strings survive round-trips intact.
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"  # type: ignore[attr-defined]

    # 3) BigInteger: SQLite's AUTOINCREMENT is only valid on a column declared
    #    as *exactly* INTEGER PRIMARY KEY. A column declared as BIGINT PRIMARY KEY
    #    is treated as NUMERIC affinity and the AUTOINCREMENT keyword is rejected
    #    (or silently ignored), causing a NOT NULL failure on the auto-generated
    #    id column at flush time. Render BigInteger as INTEGER — SQLite stores
    #    integers up to 8 bytes regardless of the declared name, so no precision
    #    is lost.
    SQLiteTypeCompiler.visit_big_integer = lambda self, type_, **kw: "INTEGER"  # type: ignore[attr-defined]

    # PG_UUID(as_uuid=True).bind_processor calls value.hex, expecting a uuid.UUID
    # object. On SQLite the ORM result processor may return a string (the raw
    # stored text), and Pydantic UUID coercion can also leave strings in edge
    # cases with from __future__ import annotations on Python 3.14. Patch the
    # bind processor to coerce strings to uuid.UUID before calling .hex.
    import uuid as _uuid_module

    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

    _orig_pg_uuid_bind = PG_UUID.bind_processor  # type: ignore[method-assign]

    def _safe_pg_uuid_bind(
        self: PG_UUID,  # type: ignore[type-arg]
        dialect: object,
    ) -> object:
        proc = _orig_pg_uuid_bind(self, dialect)
        if proc is None:
            return None

        def _safe(value: object) -> object:
            if value is not None and isinstance(value, str):
                value = _uuid_module.UUID(value)
            return proc(value)  # type: ignore[operator]

        return _safe

    PG_UUID.bind_processor = _safe_pg_uuid_bind  # type: ignore[method-assign]

    # Importing main triggers all model imports → Base.metadata is fully populated.
    from control_plane.db.models import Base
    from control_plane.db.session import engine
    from control_plane.main import app  # side-effects: populates Base.metadata

    # Create schema (create_all is idempotent via checkfirst=True).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Seed the dev customer row and developer API key (idempotent).
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO customers (id, name, created_at) "
                "VALUES (:id, :name, CURRENT_TIMESTAMP)"
            ),
            {"id": _CUSTOMER_ID, "name": "Local Dev"},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO api_keys "
                "(id, customer_id, name, role, key_hash, created_at) "
                "VALUES (:id, :cid, :name, :role, :hash, CURRENT_TIMESTAMP)"
            ),
            {
                "id": _API_KEY_ID,
                "cid": _CUSTOMER_ID,
                "name": _API_KEY_NAME,
                "role": "developer",
                "hash": _sha256(_DEV_DEVELOPER_KEY),
            },
        )
        # Operator-role key so demo scenarios can exercise operator-only
        # endpoints (e.g. POST /terminate for the manual-kill scenario).
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO api_keys "
                "(id, customer_id, name, role, key_hash, created_at) "
                "VALUES (:id, :cid, :name, :role, :hash, CURRENT_TIMESTAMP)"
            ),
            {
                "id": _OPERATOR_KEY_ID,
                "cid": _CUSTOMER_ID,
                "name": _OPERATOR_KEY_NAME,
                "role": "operator",
                "hash": _sha256(_DEV_OPERATOR_KEY),
            },
        )

    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    serve_task: asyncio.Task[Any] = asyncio.create_task(server.serve())

    # Poll /healthz until the server is accepting requests (max 10s).
    deadline = asyncio.get_event_loop().time() + 10.0
    async with httpx.AsyncClient() as probe:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await probe.get("http://127.0.0.1:8000/healthz", timeout=1.0)
                if resp.status_code == 200:
                    return server, serve_task
            except Exception:
                pass
            await asyncio.sleep(0.1)

    serve_task.cancel()
    raise RuntimeError("control plane did not become ready within 10s")
