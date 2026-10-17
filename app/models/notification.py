"""In-app notifications."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import StrEnum, TimestampMixin, utcnow


class NotificationKind(StrEnum):
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_DUE_SOON = "TASK_DUE_SOON"
    TASK_OVERDUE = "TASK_OVERDUE"
    SUBMISSION_RECEIVED = "SUBMISSION_RECEIVED"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKER_RAISED = "BLOCKER_RAISED"
    BLOCKER_RESOLVED = "BLOCKER_RESOLVED"
    COMMENT_ADDED = "COMMENT_ADDED"
    XP_AWARDED = "XP_AWARDED"
    XP_DEDUCTED = "XP_DEDUCTED"
    ACHIEVEMENT_EARNED = "ACHIEVEMENT_EARNED"
    BADGE_AWARDED = "BADGE_AWARDED"
    BADGE_REVOKED = "BADGE_REVOKED"
    LEVEL_UP = "LEVEL_UP"
    SUGGESTION_STATUS = "SUGGESTION_STATUS"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    INVITATION_ACCEPTED = "INVITATION_ACCEPTED"
    ACCOUNT_CHANGED = "ACCOUNT_CHANGED"


KIND_ICONS: dict[str, str] = {
    NotificationKind.TASK_ASSIGNED.value: "task",
    NotificationKind.TASK_DUE_SOON.value: "clock",
    NotificationKind.TASK_OVERDUE.value: "alert",
    NotificationKind.SUBMISSION_RECEIVED.value: "inbox",
    NotificationKind.REVIEW_APPROVED.value: "check",
    NotificationKind.CHANGES_REQUESTED.value: "revise",
    NotificationKind.BLOCKER_RAISED.value: "blocker",
    NotificationKind.BLOCKER_RESOLVED.value: "check",
    NotificationKind.COMMENT_ADDED.value: "comment",
    NotificationKind.XP_AWARDED.value: "xp",
    NotificationKind.XP_DEDUCTED.value: "xp",
    NotificationKind.ACHIEVEMENT_EARNED.value: "achievement",
    NotificationKind.BADGE_AWARDED.value: "badge",
    NotificationKind.BADGE_REVOKED.value: "badge",
    NotificationKind.LEVEL_UP.value: "level",
    NotificationKind.SUGGESTION_STATUS.value: "idea",
    NotificationKind.ANNOUNCEMENT.value: "megaphone",
    NotificationKind.INVITATION_ACCEPTED.value: "person",
    NotificationKind.ACCOUNT_CHANGED.value: "settings",
}


class Notification(TimestampMixin, db.Model):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Internal target only. Never an attacker-supplied URL.
    link_endpoint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    link_params: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Optional polymorphic pointer to the subject of the notification.
    subject_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Prevents duplicate notifications for the same event.
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    user = relationship("User", foreign_keys=[user_id])
    actor = relationship("User", foreign_keys=[actor_id])

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    @property
    def icon(self) -> str:
        return KIND_ICONS.get(self.kind, "bell")

    @property
    def url(self) -> str | None:
        """Resolve to a URL, but only for known internal endpoints."""
        if not self.link_endpoint:
            return None
        from flask import url_for

        params: dict[str, str] = {}
        if self.link_params:
            for chunk in self.link_params.split("&"):
                if "=" in chunk:
                    key, value = chunk.split("=", 1)
                    if key.isidentifier():
                        params[key] = value
        try:
            return url_for(self.link_endpoint, **params)
        except Exception:
            return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification {self.id} user={self.user_id} {self.kind}>"
