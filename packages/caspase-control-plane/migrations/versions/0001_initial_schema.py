"""initial schema and dev seed

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-15

Creates the foundational tables for M1: customers, api_keys, agents, events.
Seeds a single dev customer with one developer key and one operator key so
local tests can authenticate from day one (per the locked auth decision in
TODO.md).

The raw dev keys are PLAINTEXT in this migration on purpose. They are
local-only seeds — never run this migration in a customer environment without
swapping them. See `docs/integration.md` (M6) for the prod onboarding flow.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Deterministic dev IDs so .env and docs can reference them by hand.
DEV_CUSTOMER_ID = UUID("11111111-1111-4111-8111-111111111111")
DEV_DEVELOPER_KEY_ID = UUID("22222222-2222-4222-8222-222222222222")
DEV_OPERATOR_KEY_ID = UUID("33333333-3333-4333-8333-333333333333")

# Plaintext dev keys. Local-only. Hashed at insert time, raw form lives in .env.
DEV_DEVELOPER_KEY = "sk_dev_developer_local_only_do_not_ship"  # noqa: S105
DEV_OPERATOR_KEY = "sk_dev_operator_local_only_do_not_ship"  # noqa: S105


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("policy_name", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="registered"),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agents_customer_id", "agents", ["customer_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Descending index for "most recent N events for this agent" — raw DDL
    # because SQLAlchemy's expression-language descending columns in indexes
    # render inconsistently across versions.
    op.execute("CREATE INDEX ix_events_agent_id_id_desc ON events (agent_id, id DESC)")

    # --- seed ---
    customers_t = sa.table(
        "customers",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
    )
    api_keys_t = sa.table(
        "api_keys",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("customer_id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("role", sa.String),
        sa.column("key_hash", sa.String),
    )

    op.bulk_insert(customers_t, [{"id": DEV_CUSTOMER_ID, "name": "Local Dev"}])
    op.bulk_insert(
        api_keys_t,
        [
            {
                "id": DEV_DEVELOPER_KEY_ID,
                "customer_id": DEV_CUSTOMER_ID,
                "name": "dev-developer",
                "role": "developer",
                "key_hash": _hash(DEV_DEVELOPER_KEY),
            },
            {
                "id": DEV_OPERATOR_KEY_ID,
                "customer_id": DEV_CUSTOMER_ID,
                "name": "dev-operator",
                "role": "operator",
                "key_hash": _hash(DEV_OPERATOR_KEY),
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_events_agent_id_id_desc", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_agents_customer_id", table_name="agents")
    op.drop_table("agents")
    op.drop_table("api_keys")
    op.drop_table("customers")
