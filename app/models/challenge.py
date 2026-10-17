"""Weekly challenges and streak tracking."""

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
from app.models.base import StrEnum, TimestampMixin, utcnow


class ChallengeMetric(StrEnum):
    TASKS_COMPLETED = "TASKS_COMPLETED"
    ON_TIME_DELIVERIES = "ON_TIME_DELIVERIES"
    FOCUS_SESSIONS = "FOCUS_SESSIONS"
    CHECKIN_STREAK = "CHECKIN_STREAK"
    REVIEWS_COMPLETED = "REVIEWS_COMPLETED"
    BLOCKERS_RESOLVED = "BLOCKERS_RESOLVED"
    SUGGESTIONS_IMPLEMENTED = "SUGGESTIONS_IMPLEMENTED"
    KNOWLEDGE_ENTRIES = "KNOWLEDGE_ENTRIES"


CHALLENGE_METRIC_LABELS: dict[str, str] = {
    ChallengeMetric.TASKS_COMPLETED.value: "Tasks accepted",
    ChallengeMetric.ON_TIME_DELIVERIES.value: "On-time deliveries",
    ChallengeMetric.FOCUS_SESSIONS.value: "Focus sessions completed",
    ChallengeMetric.CHECKIN_STREAK.value: "Consecutive daily check-ins",
    ChallengeMetric.REVIEWS_COMPLETED.value: "Reviews completed",
    ChallengeMetric.BLOCKERS_RESOLVED.value: "Blockers resolved",
    ChallengeMetric.SUGGESTIONS_IMPLEMENTED.value: "Suggestions implemented",
    ChallengeMetric.KNOWLEDGE_ENTRIES.value: "Knowledge entries published",
}


class Challenge(TimestampMixin, db.Model):
    """A weekly challenge definition, scoped to the whole company or one member."""

    __tablename__ = "challenges"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    target_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reward_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    week_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # NULL means the challenge is open to every member.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    user = relationship("User", foreign_keys=[user_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    progress = relationship(
        "ChallengeProgress", back_populates="challenge", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_challenges_week_active", "week_start", "is_active"),
    )

    @property
    def metric_label(self) -> str:
        return CHALLENGE_METRIC_LABELS.get(self.metric, self.metric.replace("_", " ").title())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Challenge {self.id} {self.title!r}>"


class ChallengeProgress(TimestampMixin, db.Model):
    __tablename__ = "challenge_progress"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    challenge_id: Mapped[int] = mapped_column(
        ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    challenge = relationship("Challenge", back_populates="progress")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("challenge_id", "user_id", name="uq_challenge_progress_member"),
    )

    @property
    def percent(self) -> int:
        if not self.challenge or not self.challenge.target_value:
            return 0
        return max(0, min(100, round(100 * self.current_value / self.challenge.target_value)))

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None
