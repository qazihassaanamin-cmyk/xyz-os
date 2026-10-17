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
    # The reason given when an idea is declined. Stored separately from the
    # general decision note because the board surfaces it to the author.
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the idea becomes real work, so the board can link to it.
    converted_to_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    converted_to_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
    comments = relationship(
        "SuggestionComment",
        back_populates="suggestion",
        cascade="all, delete-orphan",
        order_by="SuggestionComment.created_at",
    )
    status_events = relationship(
        "SuggestionStatusEvent",
        back_populates="suggestion",
        cascade="all, delete-orphan",
        order_by="SuggestionStatusEvent.created_at",
    )

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


class SuggestionComment(TimestampMixin, db.Model):
    """A comment on a suggestion. Kept separate from the suggestion body so the
    original idea is never rewritten by discussion."""

    __tablename__ = "suggestion_comments"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(
        ForeignKey("suggestions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Comments on an anonymous suggestion are attributed: only the idea is
    # anonymous, not the discussion around it.
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    hidden_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hidden_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    suggestion = relationship("Suggestion", back_populates="comments")
    author = relationship("User", foreign_keys=[author_id])

    __table_args__ = (Index("ix_suggestion_comments_suggestion_created", "suggestion_id", "created_at"),)


class SuggestionStatusEvent(TimestampMixin, db.Model):
    """Append-only record of every status change on a suggestion.

    The current status is a column on ``suggestions``; this table is the history
    behind it, so a decision can always be reconstructed with its reason and the
    person who made it.
    """

    __tablename__ = "suggestion_status_events"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(
        ForeignKey("suggestions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    suggestion = relationship("Suggestion", back_populates="status_events")
    actor = relationship("User", foreign_keys=[actor_id])

    __table_args__ = (
        Index("ix_suggestion_status_events_suggestion_created", "suggestion_id", "created_at"),
    )


class SuggestionAnonVote(TimestampMixin, db.Model):
    """One anonymous vote per salted voter hash per suggestion.

    ``voter_hash`` is a salted digest, so the same member voting twice is
    detectable without the row being joinable back to an account. It is
    deliberately not a foreign key: nothing here can be traced to a person.
    """

    __tablename__ = "suggestion_anon_votes"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(
        ForeignKey("suggestions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    voter_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    suggestion = relationship("Suggestion")

    __table_args__ = (
        UniqueConstraint("suggestion_id", "voter_hash", name="uq_suggestion_anon_vote"),
    )


# ---------------------------------------------------------------------------
# Inquiries
# ---------------------------------------------------------------------------

class InquiryStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


INQUIRY_STATUS_LABELS = {
    InquiryStatus.OPEN.value: "Open",
    InquiryStatus.ACKNOWLEDGED.value: "Acknowledged",
    InquiryStatus.RESOLVED.value: "Resolved",
    InquiryStatus.CLOSED.value: "Closed",
}


class Inquiry(TimestampMixin, db.Model):
    """An inquiry raised by the CEO to one or more founders.

    Unlike the suggestion board, an inquiry is not an idea for the company: it
    is a question about compliance, delivery or conduct that needs a written
    answer from the founder. Every member can read the board; only the CEO can
    raise an inquiry; only the named founder can reply.

    The status is a column on the row and a history table records each change,
    so a resolution can always be reconstructed with its reason.
    """

    __tablename__ = "inquiries"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    raised_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=InquiryStatus.OPEN.value, index=True
    )
    # The founder this inquiry is addressed to. NULL means it is company-wide.
    addressed_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    raised_by = relationship("User", foreign_keys=[raised_by_id])
    addressed_to = relationship("User", foreign_keys=[addressed_to_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    replies = relationship(
        "InquiryReply",
        back_populates="inquiry",
        cascade="all, delete-orphan",
        order_by="InquiryReply.created_at",
    )
    status_events = relationship(
        "InquiryStatusEvent",
        back_populates="inquiry",
        cascade="all, delete-orphan",
        order_by="InquiryStatusEvent.created_at",
    )

    __table_args__ = (
        Index("ix_inquiries_status_created", "status", "created_at"),
        Index("ix_inquiries_addressed_to", "addressed_to_id"),
    )

    @property
    def status_label(self) -> str:
        return INQUIRY_STATUS_LABELS.get(self.status, self.status.title())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Inquiry {self.id} {self.subject[:40]!r} {self.status}>"


class InquiryReply(TimestampMixin, db.Model):
    """A reply to an inquiry, written by the founder it is addressed to.

    Replies are attributed and ordered. The original inquiry body is never
    rewritten by discussion, so the question the founder was asked stays
    visible beside their answer.
    """

    __tablename__ = "inquiry_replies"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    inquiry = relationship("Inquiry", back_populates="replies")
    author = relationship("User", foreign_keys=[author_id])

    __table_args__ = (
        Index("ix_inquiry_replies_inquiry_created", "inquiry_id", "created_at"),
    )


class InquiryStatusEvent(TimestampMixin, db.Model):
    """Append-only record of every status change on an inquiry."""

    __tablename__ = "inquiry_status_events"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    inquiry = relationship("Inquiry", back_populates="status_events")
    actor = relationship("User", foreign_keys=[actor_id])
