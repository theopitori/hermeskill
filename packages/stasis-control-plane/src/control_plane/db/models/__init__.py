"""SQLAlchemy 2.0 declarative models.

M1 lands the foundational tables: customers, api_keys, agents, events. M2.5
adds kill_events. M3 adds feedback_tokens. M5 adds apoptosis_grants.

ID convention: 16-byte UUIDv7 stored as Postgres `uuid`. UUIDv7 is monotonic
(time-prefixed) so it indexes well for our access patterns.
"""

from __future__ import annotations

from .base import Base
from .core import Agent, AgentStatus, ApiKey, ApiKeyRole, Customer, Event
from .feedback import FeedbackToken
from .grants import ApoptosisGrant
from .kill_events import KillEvent, KillEventStatus, TriggerType

__all__ = [
    "Agent",
    "AgentStatus",
    "ApiKey",
    "ApiKeyRole",
    "ApoptosisGrant",
    "Base",
    "Customer",
    "Event",
    "FeedbackToken",
    "KillEvent",
    "KillEventStatus",
    "TriggerType",
]
