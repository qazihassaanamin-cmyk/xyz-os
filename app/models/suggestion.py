"""Suggestions and the suggestion board."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class SuggestionStatus(StrEnum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    PLANNED = "PLANNED"
    IMPLEMENTED = "IMPLEMENTED"
    DECLINED = "DECLINED"


class SuggestionCategory(StrEnum):
    PROCESS = "PROCESS"
    PRODUCT = "PRODUCT"
    TOOLING = "TOOLING"
    CULTURE = "CULTURE"
    CLIENT = "CLIENT"
    OTHER = "OTHER"


SUGGESTION_STATUS_LABELS = {
    SuggestionStatus.OPEN.value: "Open",
    SuggestionStatus.UNDER_REVIEW.value: "Under review",
    SuggestionStatus.PLANNED.value: "Planned",
    SuggestionStatus.IMPLEMENTED.value: "Implemented",
    SuggestionStatus.DECLINED.value: "Declined",
}


class Suggestion(TimestampMixin, db.Model):
    """An idea raised by a member.

    ``author_id`` is set for attributed suggestions. ``is_anonymous`` is stored
    so the board can hide the author while the CEO retains accountability; the
    author is never hidden from the audit trail.
    """

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SuggestionCategory.PROCESS.value, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SuggestionStatus.OPEN.value, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Filled when the CEO decides; mandatory when declining or implementing.
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    implemented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the implementation XP has been granted to the author.
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    author = relationship("User", foreign_keys=[author_id])
    decided_by = relationship("User", foreign_keys=[decided_by_id])
    project = relationship("Project", foreign_keys=[project_id])
    votes = relationship("SuggestionVote", back_populates="suggestion", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_suggestions_status_created", "status", "created_at"),
    )

    @property
    def status_label(self) -> str:
        return SUGGESTION_STATUS_LABELS.get(self.status, self.status.title())

    @property
    def display_author(self) -> str:
        if self.is_anonymous:
            return "Anonymous member"
        return self.author.display_name if self.author else "Former member"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Suggestion {self.id} {self.title[:40]!r} {self.status}>"


class SuggestionVote(TimestampMixin, db.Model):
    """One supporting vote per member per suggestion."""

    __tablename__ = "suggestion_votes"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(
        ForeignKey("suggestions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stable pseudonym so anonymous voting stays consistent without exposing ids.
    voter_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    suggestion = relationship("Suggestion", back_populates="votes")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("suggestion_id", "user_id", name="uq_suggestion_vote_member"),
    )
