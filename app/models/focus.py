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
from app.models.base import StrEnum, TimestampMixin, as_utc


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
    # Portable uniqueness guards.
    #
    # PostgreSQL and SQLite support partial unique indexes; MySQL does not. To
    # keep the rules identical on every dialect these two columns are plain
    # UNIQUE columns that stay NULL unless the row is in a state that must be
    # unique. A NULL never collides in any dialect's unique index, so:
    #
    #   open_key   = "user:<id>"  while RUNNING or PAUSED, NULL otherwise
    #   credit_key = "user:<id>:<workday>"  for the one credited session, else NULL
    #
    # The service layer clears them as the session changes state, and the tests
    # assert the behaviour on SQLite while documenting the MySQL/Postgres
    # equivalence.
    open_key: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    credit_key: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    interrupted_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Set once focus XP has been granted (idempotency guard in the ledger).
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    task = relationship("Task", back_populates="focus_sessions")

    __table_args__ = (
        # Two open sessions for one member would make "the current session"
        # ambiguous, and two credited sessions in one workday would inflate the
        # daily figure. Both rules are enforced by the database itself through
        # the portable guards above, so they hold even under concurrent requests.
        Index("ix_focus_user_started", "user_id", "started_at"),
        Index("ix_focus_workday_status", "workday_date", "status"),
    )

    @property
    def open_key_value(self) -> str:
        return f"user:{self.user_id}"

    @property
    def credit_key_value(self) -> str:
        return f"user:{self.user_id}:{self.workday_date.isoformat()}"

    def elapsed_seconds(self, now: datetime | None = None) -> int:
        """Authoritative elapsed time, computed from server clock only."""
        from app.models.base import utcnow as _utcnow

        now = now or _utcnow()
        total = int(self.accumulated_seconds or 0)
        if self.status == FocusStatus.RUNNING.value and self.last_resumed_at is not None:
            resumed = as_utc(self.last_resumed_at)
            if resumed is not None:
                total += max(0, int((now - resumed).total_seconds()))
        return total

    @property
    def is_credited(self) -> bool:
        """True when this session earned the day's single focus credit."""
        return bool(self.credited) and self.status == FocusStatus.COMPLETED.value

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
