"""feedback_tokens table (M3 — one-click feedback)

Revision ID: 0003_feedback_tokens
Revises: 0002_kill_events
Create Date: 2026-05-22

Adds `feedback_tokens` — the lookup table for unauthenticated one-click
feedback URLs sent in the death certificate. The PK is the SHA-256 hex
of the raw token; raw tokens are never persisted (same posture as
`api_keys.key_hash`). `kill_event_id` is UNIQUE — one token per cert.

Single-use: `used_at` is set on first successful POST /feedback/{token}.
A second submission returns 410. Expiry is enforced application-side
against `expires_at` (default 30 days from issue).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_feedback_tokens"
down_revision: str | Sequence[str] | None = "0002_kill_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "kill_event_id",
            sa.BigInteger,
            sa.ForeignKey("kill_events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("feedback_tokens")
