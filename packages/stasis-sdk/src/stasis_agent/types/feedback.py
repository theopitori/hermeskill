"""Feedback submission types (M3 one-click feedback)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .enums import FeedbackLabel


class FeedbackIn(BaseModel):
    """POST body for /feedback/{token}.

    Unauthenticated — the token in the URL is the auth. Single-use:
    a second submission for the same token returns 410.
    """

    model_config = ConfigDict(extra="forbid")

    label: FeedbackLabel


class FeedbackOut(BaseModel):
    """Acknowledgement of a feedback submission."""

    kill_event_id: int
    label: FeedbackLabel
    received_at: datetime
