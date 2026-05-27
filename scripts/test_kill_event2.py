"""Minimal test: insert kill_event directly via ORM to see the exact error."""
import asyncio
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_PATH = Path("C:/Temp/caspase-test2.db")


async def main() -> None:
    DB_PATH.unlink(missing_ok=True)
    os.environ["CASPASE_DB_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"

    import uuid as _uuid_module

    from sqlalchemy import JSON, text
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.process(JSON(), **kw)  # type: ignore
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"  # type: ignore
    SQLiteTypeCompiler.visit_big_integer = lambda self, type_, **kw: "INTEGER"  # type: ignore

    _orig = PG_UUID.bind_processor  # type: ignore

    def _safe(self, dialect):  # type: ignore
        proc = _orig(self, dialect)
        if proc is None:
            return None
        def inner(value):
            if value is not None and isinstance(value, str):
                value = _uuid_module.UUID(value)
            return proc(value)
        return inner

    PG_UUID.bind_processor = _safe  # type: ignore

    from control_plane.db.models import (
        Agent,
        AgentStatus,
        Base,
        KillEvent,
        KillEventStatus,
        TriggerType,
    )
    from control_plane.db.session import SessionLocal, engine

    CUSTOMER_ID = "11111111-1111-4111-8111-111111111111"
    API_KEY_ID = "22222222-2222-4222-8222-222222222222"
    DEV_KEY = "sk_dev_developer_local_only_do_not_ship"

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("INSERT OR IGNORE INTO customers (id, name, created_at) VALUES (:id, :name, CURRENT_TIMESTAMP)"),
            {"id": CUSTOMER_ID, "name": "Local Dev"},
        )
        await conn.execute(
            text("INSERT OR IGNORE INTO api_keys (id, customer_id, name, role, key_hash, created_at) VALUES (:id, :cid, :name, :role, :hash, CURRENT_TIMESTAMP)"),
            {"id": API_KEY_ID, "cid": CUSTOMER_ID, "name": "Demo", "role": "developer", "hash": hashlib.sha256(DEV_KEY.encode()).hexdigest()},
        )

    import uuid

    # Create an agent directly
    agent_uuid = uuid.uuid4()
    async with SessionLocal() as session:
        agent = Agent(
            id=agent_uuid,
            customer_id=uuid.UUID(CUSTOMER_ID),
            policy_name="coding-default",
            name="test-agent",
            status=AgentStatus.REGISTERED,
            metadata_={},
        )
        session.add(agent)
        await session.commit()
        print("Agent created, id:", agent.id)

    # Now try to create a kill_event
    async with SessionLocal() as session:
        now = datetime.now(UTC)
        kill_event = KillEvent(
            agent_id=agent_uuid,
            trigger_type=TriggerType.AUTO,
            trigger_reason="test loop",
            triggered_at=now,
            terminated_at=now,
            status=KillEventStatus.CONFIRMED,
            death_certificate={"test": True},
            shutdown_log=[],
        )
        session.add(kill_event)
        try:
            await session.flush()
            print("flush succeeded, kill_event.id =", kill_event.id)
            await session.commit()
            print("commit succeeded")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            await session.rollback()


asyncio.run(main())
