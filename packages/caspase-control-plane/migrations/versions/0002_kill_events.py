"""kill_events table (M2.5 — the death certificate)

Revision ID: 0002_kill_events
Revises: 0001_initial
Create Date: 2026-05-17

Adds `kill_events`, the most important table the control plane owns —
the forensic record of every agent that has died. Each row stores the
trigger (auto symptom vs manual operator kill), the death certificate
(jsonb), the shutdown log (jsonb), and the lifecycle status.

Partial unique constraint `ux_kill_events_one_active_per_agent`
prevents the symptom-kill vs manual-kill race: at most one kill_event
can be in flight per agent at any time. Conflicting POSTs return
409 with the existing row's id — the SDK is expected to treat 409
as "already dying, fine."

The status column intentionally allows transitions via UPDATE rather
than INSERT-only (M4 will mark initiated → confirmed/zombie from a
heartbeat sweeper). The CHECK constraint enumerates the legal values
defensively (in addition to the application-side enum) — bad data in
this table is catastrophic for audit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_kill_events"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kill_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "trigger_type",
            sa.String(20),
            nullable=False,
        ),
        sa.Column(
            "trigger_reason",
            sa.String(500),
            nullable=False,
        ),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "terminated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="initiated",
        ),
        # M4 fills these for operator-initiated kills.
        sa.Column(
            "operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("operator_reason", sa.String(2000), nullable=True),
        # The forensic payload. NULL until the SDK posts the cert; sweeper
        # may mark status=confirmed even without a cert present.
        sa.Column(
            "death_certificate",
            postgresql.JSONB,
            nullable=True,
        ),
        # Append-only structured log of the shutdown sequence.
        sa.Column(
            "shutdown_log",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # M3 — operator feedback ("was this kill correct?").
        sa.Column("feedback_label", sa.String(40), nullable=True),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Enumerate legal status values defensively. The enum is enforced
        # in Pydantic too, but bad data here is catastrophic for audit so
        # we want a database-side floor.
        sa.CheckConstraint(
            "status IN ('initiated', 'confirmed', 'zombie')",
            name="ck_kill_events_status_enum",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('auto', 'manual')",
            name="ck_kill_events_trigger_type_enum",
        ),
    )
    # Fast list-by-agent lookup for `caspase logs` / future CLI commands.
    op.create_index("ix_kill_events_agent_id", "kill_events", ["agent_id"])

    # **The race-prevention constraint** — at most one active (initiated
    # or confirmed) kill_event per agent. Zombies are excluded because
    # the agent didn't actually die so a follow-up kill is sensible.
    # Raw DDL because Alembic's `create_index(..., postgresql_where=...)`
    # has historically been finicky across versions.
    op.execute(
        "CREATE UNIQUE INDEX ux_kill_events_one_active_per_agent "
        "ON kill_events (agent_id) "
        "WHERE status IN ('initiated', 'confirmed')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_kill_events_one_active_per_agent")
    op.drop_index("ix_kill_events_agent_id", table_name="kill_events")
    op.drop_table("kill_events")
