"""Quick diagnostic: test kill_event posting against the patched SQLite backend."""
import asyncio
import hashlib
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.DEBUG)

DB_PATH = Path("C:/Temp/hermeskill-test.db")
CUSTOMER_ID = "11111111-1111-4111-8111-111111111111"
API_KEY_ID = "22222222-2222-4222-8222-222222222222"
DEV_KEY = "sk_dev_developer_local_only_do_not_ship"


async def main() -> None:
    DB_PATH.unlink(missing_ok=True)
    os.environ["HERMESKILL_DB_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"

    import uuid as _uuid_module

    from sqlalchemy import JSON, text
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.process(  # type: ignore[attr-defined]
            JSON(), **kw
        )
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"  # type: ignore[attr-defined]

    _orig = PG_UUID.bind_processor  # type: ignore[method-assign]

    def _safe(self: PG_UUID, dialect: object) -> object:  # type: ignore[type-arg]
        proc = _orig(self, dialect)
        if proc is None:
            return None

        def inner(value: object) -> object:
            if value is not None and isinstance(value, str):
                value = _uuid_module.UUID(value)
            return proc(value)  # type: ignore[operator]

        return inner

    PG_UUID.bind_processor = _safe  # type: ignore[method-assign]

    from control_plane.db.models import Base
    from control_plane.db.session import engine
    from control_plane.main import app

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO customers (id, name, created_at) "
                "VALUES (:id, :name, CURRENT_TIMESTAMP)"
            ),
            {"id": CUSTOMER_ID, "name": "Local Dev"},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO api_keys "
                "(id, customer_id, name, role, key_hash, created_at) "
                "VALUES (:id, :cid, :name, :role, :hash, CURRENT_TIMESTAMP)"
            ),
            {
                "id": API_KEY_ID,
                "cid": CUSTOMER_ID,
                "name": "Demo",
                "role": "developer",
                "hash": hashlib.sha256(DEV_KEY.encode()).hexdigest(),
            },
        )

    import httpx
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agents",
            json={"name": "test", "policy_name": "coding-default", "metadata": {}},
            headers={"Authorization": f"Bearer {DEV_KEY}"},
        )
        print("register:", resp.status_code, resp.text[:300])
        if resp.status_code != 201:
            return
        agent_id = resp.json()["agent_id"]
        print("agent_id:", agent_id)

        now = datetime.now(UTC).isoformat()
        cert_payload = {
            "trigger_type": "auto",
            "trigger_reason": "test loop",
            "triggered_at": now,
            "terminated_at": now,
            "death_certificate": {
                "agent_id": agent_id,
                "trigger_type": "auto",
                "trigger_reason": "test loop",
                "triggered_at": now,
                "terminated_at": now,
                "symptoms_log": [],
                "final_state": {},
                "shutdown_log": [],
            },
            "shutdown_log": [],
        }
        resp2 = await client.post(
            f"/agents/{agent_id}/kill_events",
            json=cert_payload,
            headers={"Authorization": f"Bearer {DEV_KEY}"},
        )
        print("kill event:", resp2.status_code, resp2.text[:1000])

        # If 201, try to GET it back
        if resp2.status_code == 201:
            ke_id = resp2.json()["id"]
            resp3 = await client.get(
                f"/kill_events/{ke_id}",
                headers={"Authorization": f"Bearer {DEV_KEY}"},
            )
            print("GET kill event:", resp3.status_code, resp3.text[:200])


asyncio.run(main())
