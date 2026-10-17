"""The Lounge: an internal conversation space for the founding team."""

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
from app.models.base import TimestampMixin, utcnow

# Editing is allowed only inside this window, enforced server-side.
EDIT_WINDOW_SECONDS = 10 * 60


class LoungePost(TimestampMixin, db.Model):
    """A message in the Lounge.

    ``edited_at`` records that a message was changed; the previous text is kept
    in ``LoungePostRevision`` so a conversation cannot be quietly rewritten.
    """

    __tablename__ = "lounge_posts"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("lounge_posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Optional project/task context so a message can be traced to work.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    hidden_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hidden_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Cheap reply counter so the list view needs no aggregate query.
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    author = relationship("User", foreign_keys=[author_id])
    parent = relationship("LoungePost", remote_side="LoungePost.id", foreign_keys=[parent_id])
    project = relationship("Project", foreign_keys=[project_id])
    task = relationship("Task", foreign_keys=[task_id])
    revisions = relationship(
        "LoungePostRevision",
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="LoungePostRevision.edited_at",
    )

    __table_args__ = (
        Index("ix_lounge_posts_created", "created_at"),
        Index("ix_lounge_posts_parent_created", "parent_id", "created_at"),
    )

    def is_editable_by(self, user) -> bool:
        """Author may edit within the window; the CEO may always hide, not edit."""
        if user is None or getattr(user, "id", None) is None:
            return False
        if self.author_id != user.id:
            return False
        if self.is_hidden:
            return False
        if self.edited_at is not None:
            return False
        if self.created_at is None:
            return False
        created = self.created_at
        if created.tzinfo is None:
            from datetime import timezone as _tz

            created = created.replace(tzinfo=_tz.utc)
        return (utcnow() - created).total_seconds() <= EDIT_WINDOW_SECONDS

    @property
    def is_edited(self) -> bool:
        return self.edited_at is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LoungePost {self.id} author={self.author_id}>"


class LoungePostRevision(TimestampMixin, db.Model):
    """Previous text of an edited Lounge message."""

    __tablename__ = "lounge_post_revisions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("lounge_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    previous_body: Mapped[str] = mapped_column(Text, nullable=False)
    edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    post = relationship("LoungePost", back_populates="revisions")
