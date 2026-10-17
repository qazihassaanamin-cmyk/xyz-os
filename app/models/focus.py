"""Focus sessions and workday records."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import StrEnum, TimestampMixin


class FocusStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    ABANDONED = "ABANDONED"


OPEN_FOCUS_STATUSES = (FocusStatus.RUNNING.value, FocusStatus.PAUSED.value)


class FocusSession(TimestampMixin, db.Model):
    """A server-authoritative focus session.

    Elapsed time is always derived from server timestamps; a duration sent by
    the browser is never trusted. ``accumulated_seconds`` holds paused-and-
    resumed banked time, ``last_resumed_at`` marks the start of the current run.
    """

    __tablename__ = "focus_sessions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    goal: Mapped[str | None] = mapped_column(String(300), nullable=True)
    workday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FocusStatus.RUNNING.value, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=None
    )
    last_resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accumulated_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set when this session counted toward the daily focus credit.
    credited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interrupted_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Set once focus XP has been granted (idempotency guard in the ledger).
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    task = relationship("Task", back_populates="focus_sessions")

    __table_args__ = (
        # One open session per user, enforced by the database itself.
        Index(
            "uq_focus_open_per_user",
            "user_id",
            unique=True,
            sqlite_where=(db.text("status IN ('RUNNING','PAUSED')")),
            postgresql_where=(db.text("status IN ('RUNNING','PAUSED')")),
        ),
        # Exactly one credited session per user per company workday.
        Index(
            "uq_focus_credited_per_user_day",
            "user_id",
            "workday_date",
            unique=True,
            sqlite_where=(db.text("status = 'COMPLETED'")),
            postgresql_where=(db.text("status = 'COMPLETED'")),
        ),
        Index("ix_focus_user_started", "user_id", "started_at"),
    )

    def elapsed_seconds(self, now: datetime | None = None) -> int:
        """Authoritative elapsed time, computed from server clock only."""
        from app.models.base import utcnow as _utcnow

        now = now or _utcnow()
        total = int(self.accumulated_seconds or 0)
        if self.status == FocusStatus.RUNNING.value and self.last_resumed_at is not None:
            total += max(0, int((now - self.last_resumed_at).total_seconds()))
        return total

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_FOCUS_STATUSES


class Workday(TimestampMixin, db.Model):
    """Per-date flag for whether the company treats a date as a working day."""

    __tablename__ = "workdays"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    is_working_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)


class WorkdayExceptionKind(StrEnum):
    ABSENCE = "ABSENCE"
    HOLIDAY = "HOLIDAY"
    OTHER = "OTHER"


class WorkdayException(TimestampMixin, db.Model):
    """Company-wide holiday/break or an approved absence for one member."""

    __tablename__ = "workday_exceptions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    # NULL means the exception applies to the whole company.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WorkdayExceptionKind.ABSENCE.value
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Set when the CEO has formally approved the record.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])

    __table_args__ = (
        UniqueConstraint("user_id", "date", "kind", name="uq_workday_exception_user_date_kind"),
    )
