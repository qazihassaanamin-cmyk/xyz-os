"""Announcements and knowledge base entries."""

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import StrEnum, TimestampMixin, as_utc, utcnow


class AnnouncementScope(StrEnum):
    COMPANY = "COMPANY"
    PROJECT = "PROJECT"


class KnowledgeStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class KnowledgeCategory(StrEnum):
    PROCESS = "PROCESS"
    TECHNICAL = "TECHNICAL"
    ONBOARDING = "ONBOARDING"
    CLIENT = "CLIENT"
    REFERENCE = "REFERENCE"
    OTHER = "OTHER"


class Announcement(TimestampMixin, db.Model):
    """Company or project notice. Written by the CEO or a project lead."""

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AnnouncementScope.COMPANY.value, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # Optional expiry so old notices fall off the dashboard on their own.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Marks recognition announcements so the profile history can separate them.
    is_recognition: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    author = relationship("User", foreign_keys=[author_id])
    project = relationship("Project", foreign_keys=[project_id])
    reads = relationship("AnnouncementRead", back_populates="announcement", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_announcements_scope_created", "scope", "created_at"),
    )

    @property
    def is_expired(self) -> bool:
        expires = as_utc(self.expires_at)
        return expires is not None and expires <= utcnow()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Announcement {self.id} {self.title[:40]!r}>"


class AnnouncementRead(TimestampMixin, db.Model):
    __tablename__ = "announcement_reads"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    announcement = relationship("Announcement", back_populates="reads")
    user = relationship("User")

    __table_args__ = (
        db.UniqueConstraint("announcement_id", "user_id", name="uq_announcement_read_member"),
    )


class KnowledgeEntry(TimestampMixin, db.Model):
    """Internal documentation. Publishing one is a verified XP-eligible event."""

    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), nullable=False, unique=True, index=True)
    summary: Mapped[str | None] = mapped_column(String(400), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default=KnowledgeCategory.TECHNICAL.value, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=KnowledgeStatus.DRAFT.value, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the documentation XP has been granted.
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Marks a note about a technical decision, which earns the decision XP tier.
    is_decision_record: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    author = relationship("User", foreign_keys=[author_id])
    project = relationship("Project", foreign_keys=[project_id])
    task = relationship("Task", foreign_keys=[task_id])

    __table_args__ = (
        Index("ix_knowledge_entries_status_created", "status", "created_at"),
    )

    @property
    def is_published(self) -> bool:
        return self.status == KnowledgeStatus.PUBLISHED.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KnowledgeEntry {self.id} {self.slug!r} {self.status}>"


class DecisionRecord(TimestampMixin, db.Model):
    """A decision the company has made, with its reasoning and consequences.

    Kept separate from the knowledge base on purpose: a knowledge entry explains
    *how* something works, a decision record explains *why* it was chosen and
    what was traded away. When a decision is revisited, the original record is
    superseded rather than rewritten, so the reasoning at the time survives.
    """

    __tablename__ = "decision_records"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), nullable=False, unique=True, index=True)
    # The situation that forced a decision.
    context: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    consequences: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What was considered and rejected, and why. Optional but encouraged.
    alternatives: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PROPOSED", index=True
    )
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Set when a later decision replaces this one.
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("decision_records.id", ondelete="SET NULL"), nullable=True
    )
    # Set once the CEO has approved the record for the knowledge base.
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    author = relationship("User", foreign_keys=[decided_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    project = relationship("Project", foreign_keys=[project_id])
    superseded_by = relationship(
        "DecisionRecord", remote_side="DecisionRecord.id", foreign_keys=[superseded_by_id]
    )

    __table_args__ = (Index("ix_decision_records_status_created", "status", "created_at"),)
