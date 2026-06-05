"""FeedbackToken model (M3)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class FeedbackToken(Base):
    """One-click feedback token (M3).

    PK is the SHA-256 hex of the raw token — same posture as `api_keys.key_hash`.
    Raw tokens only appear in the feedback URL embedded in the death cert;
    they are never persisted. `kill_event_id` is UNIQUE so a cert has exactly
    one token. Single-use: `used_at` is stamped on the first successful POST
    /feedback/{token}; subsequent submissions return 410.
    """

    __tablename__ = "feedback_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kill_event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kill_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
