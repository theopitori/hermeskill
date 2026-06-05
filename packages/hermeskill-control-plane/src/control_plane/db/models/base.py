"""SQLAlchemy declarative base shared by all models."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all Hermeskill control-plane models."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB().with_variant(JSON(), "sqlite"),
    }
