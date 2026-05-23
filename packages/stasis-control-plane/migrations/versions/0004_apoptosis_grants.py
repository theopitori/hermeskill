"""apoptosis_grants table (M5)

Revision ID: 0004_apoptosis_grants
Revises: 0003_feedback_tokens
Create Date: 2026-05-23

A grant is an operator's permission for an agent to survive a specific
set of symptoms for a bounded time. The SDK caches active grants (via
the heartbeat response) and demotes covered Terminal symptoms into
Warnings before they trip the apoptosis flag.

Active = `revoked_at IS NULL AND expires_at > now()`. We don't physically
delete revoked/expired grants — the death cert may reference them by id
for audit, and "did a grant suppress what would have killed this agent?"
is a question operators answer post-mortem.

Validation lives in the API layer ([api/grants.py]): symptoms must be a
subset of the policy's `apoptosis_proofing.allowed_symptoms`, manual_kill
is never grantable, and `expires_at - issued_at` is capped at 24h
regardless of policy. The DB only enforces the structural shape.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_apoptosis_grants"
down_revision: str | Sequence[str] | None = "0003_feedback_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apoptosis_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # JSONB list of SymptomType values. Validated app-side; the DB
        # only stores. The list-of-strings shape is what `active_grants`
        # carries back to the SDK over heartbeat.
        sa.Column("symptoms", postgresql.JSONB, nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column(
            "issued_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revoke_reason", sa.String(2000), nullable=True),
    )
    # Hot path: "give me the active grants for this agent" — fires on every
    # heartbeat.
    op.create_index(
        "ix_apoptosis_grants_agent_id_active",
        "apoptosis_grants",
        ["agent_id", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_apoptosis_grants_agent_id_active", table_name="apoptosis_grants"
    )
    op.drop_table("apoptosis_grants")
