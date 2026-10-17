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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin, as_utc, utcnow

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
    # Named topic so the conversation can be separated later (general, tech,
    # wins, random) without changing the message table.
    topic: Mapped[str] = mapped_column(String(32), nullable=False, default="general", index=True)
    # The room the message belongs to. Defaults to the single launch room.
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_rooms.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Idempotency guard for retried sends: a client that retries after a dropped
    # response must not create a second copy of the same message.
    client_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Named ``reply_to_id`` to match the brief; ``parent_id`` was the earlier name.
    reply_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("lounge_posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    hidden_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hidden_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Cheap reply counter so the list view needs no aggregate query.
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    author = relationship("User", foreign_keys=[author_id])
    parent = relationship("LoungePost", remote_side="LoungePost.id", foreign_keys=[parent_id])
    room = relationship("ChatRoom", back_populates="posts")
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
        # A retried send with the same client id from the same author is the same
        # message, so the database refuses the duplicate rather than the UI
        # hoping the retry does not happen.
        UniqueConstraint("author_id", "client_msg_id", name="uq_lounge_post_client_msg"),
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
        created = as_utc(self.created_at)
        if created is None:
            return False
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


class ChatRoom(TimestampMixin, db.Model):
    """A named room in the Lounge.

    Rooms exist so the conversation can be separated later (for example a room
    per project) without changing the message table. At launch there is a single
    default room, created by ``ensure_default_room``.
    """

    __tablename__ = "chat_rooms"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_by = relationship("User", foreign_keys=[created_by_id])
    posts = relationship("LoungePost", back_populates="room")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatRoom {self.slug!r}>"


class LoungeReadState(TimestampMixin, db.Model):
    """How far a member has read in a room.

    Storing the highest message id the member has seen is what makes the unread
    count exact without scanning the message table on every page load.
    """

    __tablename__ = "lounge_read_state"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_id: Mapped[int] = mapped_column(
        ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_read_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    room = relationship("ChatRoom")

    __table_args__ = (
        UniqueConstraint("user_id", "room_id", name="uq_lounge_read_state_member_room"),
    )


DEFAULT_ROOM_SLUG = "the-lounge"


def ensure_default_room() -> ChatRoom:
    """Return the default room, creating it once. Idempotent."""
    from app.extensions import db as _db

    room = ChatRoom.query.filter_by(slug=DEFAULT_ROOM_SLUG).first()
    if room is not None:
        return room
    room = ChatRoom(
        name="The Lounge",
        slug=DEFAULT_ROOM_SLUG,
        description="General conversation for the team.",
        is_default=True,
    )
    _db.session.add(room)
    try:
        _db.session.commit()
    except Exception:  # pragma: no cover - a concurrent request created it
        _db.session.rollback()
        room = ChatRoom.query.filter_by(slug=DEFAULT_ROOM_SLUG).first()
    return room
