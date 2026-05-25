"""One-shot verification script for M1.1: confirms tables + seed exist.

Run: `uv run python scripts/verify_m1_1.py` with CASPASE_DB_URL set.
"""

from __future__ import annotations

import asyncio
import os
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> int:
    url = os.environ["CASPASE_DB_URL"]
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        # 1. Tables exist
        tables = (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        ))).scalars().all()
        print("tables:", tables)
        expected = {"alembic_version", "agents", "api_keys", "customers", "events"}
        missing = expected - set(tables)
        if missing:
            print(f"MISSING TABLES: {missing}")
            return 1

        # 2. Seed customer
        cust = (await conn.execute(text(
            "SELECT id, name FROM customers"
        ))).fetchall()
        print("customers:", cust)
        if len(cust) != 1 or cust[0][1] != "Local Dev":
            print("expected exactly one 'Local Dev' customer")
            return 1

        # 3. Seed keys (verify hash by re-hashing the known raw value)
        keys = (await conn.execute(text(
            "SELECT name, role, key_hash FROM api_keys ORDER BY name"
        ))).fetchall()
        print("api_keys:", keys)
        expected_dev = sha256(b"sk_dev_developer_local_only_do_not_ship").hexdigest()
        expected_op = sha256(b"sk_dev_operator_local_only_do_not_ship").hexdigest()
        keys_by_name = {k[0]: k for k in keys}
        if keys_by_name["dev-developer"][2] != expected_dev:
            print("dev-developer key_hash mismatch")
            return 1
        if keys_by_name["dev-operator"][2] != expected_op:
            print("dev-operator key_hash mismatch")
            return 1
        if keys_by_name["dev-developer"][1] != "developer":
            print("dev-developer role wrong")
            return 1
        if keys_by_name["dev-operator"][1] != "operator":
            print("dev-operator role wrong")
            return 1

        # 4. Indexes exist
        idx = (await conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('events','agents') "
            "ORDER BY indexname"
        ))).scalars().all()
        print("indexes:", idx)
        if "ix_events_agent_id_id_desc" not in idx:
            print("missing ix_events_agent_id_id_desc")
            return 1

    await engine.dispose()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
