"""Daily accountability: check-ins, end-of-day reports and revisions."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin


class DailyCheckin(TimestampMixin, db.Model):
    __tablename__ = "daily_checkins"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    priorities: Mapped[str] = mapped_column(Text, nullable=False)
    intended_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    support_needed: Mapped[str | None] = mapped_column(Text, nullable=True)
    blockers: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the check-in XP has been granted.
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "workday_date", name="uq_daily_checkin_user_date"),
        Index("ix_daily_checkin_date_user", "workday_date", "user_id"),
    )


class DailyReport(TimestampMixin, db.Model):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    accomplished: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    unfinished: Mapped[str | None] = mapped_column(Text, nullable=True)
    unfinished_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Counts every save after the first, so the UI can show "edited N times".
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    revisions = relationship(
        "DailyReportRevision",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="DailyReportRevision.edited_at",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "workday_date", name="uq_daily_report_user_date"),
        Index("ix_daily_report_date_user", "workday_date", "user_id"),
    )


class DailyReportRevision(TimestampMixin, db.Model):
    """Immutable snapshot of a report as it stood before an edit."""

    __tablename__ = "daily_report_revisions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("daily_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    report = relationship("DailyReport", back_populates="revisions")
