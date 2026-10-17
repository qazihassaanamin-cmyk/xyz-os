"""Shared model primitives: timestamps, soft delete, enum helpers."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


def utcnow() -> datetime:
    """Timezone-aware UTC now. Every stored timestamp goes through this."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` as an aware UTC datetime.

    SQLite has no native timestamp type and hands back naive datetimes even for
    a column declared ``DateTime(timezone=True)``, while PostgreSQL returns
    aware ones. Every comparison against ``utcnow()`` therefore goes through
    this helper, which keeps the two backends behaving identically instead of
    raising "can't compare offset-naive and offset-aware datetimes" on SQLite.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_past(value: datetime | None) -> bool:
    """True when ``value`` is in the past. ``None`` is never in the past."""
    normalised = as_utc(value)
    return normalised is not None and normalised <= utcnow()


def is_future(value: datetime | None) -> bool:
    normalised = as_utc(value)
    return normalised is not None and normalised > utcnow()


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` maintained in UTC.

    ``onupdate`` is applied at the Python level so SQLite and PostgreSQL behave
    identically and so ORM-side change tracking stays deterministic.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class SoftDeleteMixin:
    """Deactivate instead of destroying people and work records."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("users.id"), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class StrEnum(str, enum.Enum):
    """String enum whose ``.value`` is stored in the database."""

    def __str__(self) -> str:  # pragma: no cover - display helper
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value):
        """Return the enum member for ``value`` or ``None`` when invalid."""
        if isinstance(value, cls):
            return value
        if value is None:
            return None
        try:
            return cls(str(value))
        except ValueError:
            return None


def enum_column(enum_cls, **kwargs):
    """A portable enum column.

    Native ``ENUM`` types behave differently on SQLite and PostgreSQL; storing
    the string value with a validation hook keeps migrations simple and both
    backends honest. Callers pass ``default=`` as the enum member.
    """
    return db.Column(
        db.String(kwargs.pop("length", 32)),
        nullable=kwargs.pop("nullable", False),
        default=kwargs.pop("default", None),
        index=kwargs.pop("index", False),
        **kwargs,
    )
