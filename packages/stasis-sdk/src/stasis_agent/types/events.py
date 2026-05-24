"""Event ingestion and retrieval types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from .enums import EventType


class EventIn(BaseModel):
    """One event posted by the SDK (tool call, llm call, lifecycle, etc.)."""

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    # Client-side timestamp; server stamps its own created_at on insert.
    occurred_at: datetime | None = None


class EventBatchIn(BaseModel):
    """SDK batches events for efficient ingest."""

    events: list[EventIn] = Field(default_factory=list, max_length=1000)


class EventOut(BaseModel):
    id: int
    agent_id: UUID
    type: EventType
    payload: dict[str, Any]
    created_at: datetime


class EventPage(BaseModel):
    """Cursor-paginated events response."""

    events: list[EventOut]
    # For paging backwards (default): the smallest id in this page; pass as
    # `before_id` to fetch the next older page.
    next_before_id: int | None = None
    # For tailing (--follow): the largest id seen; pass as `after_id` to poll
    # for events newer than this.
    last_id: int | None = None
