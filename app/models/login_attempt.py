"""Login attempt tracking, used for throttling and for the activity record."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TimestampMixin, utcnow


class LoginAttempt(TimestampMixin, db.Model):
    """One row per login attempt. Rows are cheap; they are never rewritten.

    ``username_tried`` is stored because a failed attempt against a
    non-existent account is exactly the signal worth having.
    """

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    username_tried: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (
        Index("ix_login_attempts_ip_created", "ip_address", "created_at"),
        Index("ix_login_attempts_user_created", "user_id", "created_at"),
    )

    @classmethod
    def recent_failures_for_user(cls, user_id: int, window_minutes: int = 15) -> int:
        since = utcnow() - timedelta(minutes=window_minutes)
        return (
            cls.query.filter(
                cls.user_id == user_id,
                cls.success.is_(False),
                cls.created_at >= since,
            ).count()
        )

    @classmethod
    def recent_failures_for_ip(cls, ip: str, window_minutes: int = 15) -> int:
        since = utcnow() - timedelta(minutes=window_minutes)
        return (
            cls.query.filter(
                cls.ip_address == ip,
                cls.success.is_(False),
                cls.created_at >= since,
            ).count()
        )
