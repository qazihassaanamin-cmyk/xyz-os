"""User, invitation and password-reset models."""

from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import StrEnum, TimestampMixin, utcnow


class UserRole(StrEnum):
    CEO = "CEO"
    MEMBER = "MEMBER"


class User(TimestampMixin, UserMixin, db.Model):
    """A member of XYZ Ltd.

    ``role`` is deliberately only CEO or MEMBER. Project-level leadership lives
    in ``project_members.role_in_project`` so a project lead never inherits
    company-wide administrative rights.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    # Official founder role and title, e.g. "Caretaker CEO and Technical Lead".
    # This is a record of the title the company uses; it is never presented as
    # proof of a formal statutory appointment.
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=UserRole.MEMBER.value)
    avatar_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active_flag: Mapped[bool] = mapped_column(
        "is_active", Boolean, nullable=False, default=True, server_default="1"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Karachi")
    # Free-text professional biography and skills, editable by the member.
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the CEO deactivates an account; sessions are rejected after this.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # --- relationships ---------------------------------------------------
    project_memberships = relationship(
        "ProjectMember", back_populates="user", cascade="save-update, merge"
    )
    owned_projects = relationship(
        "Project", back_populates="owner", foreign_keys="Project.owner_id"
    )
    assigned_tasks = relationship(
        "Task", back_populates="assignee", foreign_keys="Task.assignee_id"
    )
    created_tasks = relationship(
        "Task", back_populates="creator", foreign_keys="Task.created_by_id"
    )
    xp_profile = relationship("XpProfile", back_populates="user", uselist=False)
    achievements = relationship("MemberAchievement", back_populates="user")

    # --- flask-login ------------------------------------------------------
    def get_id(self) -> str:  # pragma: no cover - trivial
        return str(self.id)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        """Flask-Login calls this; deactivated members must not authenticate."""
        return bool(self.is_active_flag) and self.deactivated_at is None

    # --- role helpers -----------------------------------------------------
    @property
    def is_ceo(self) -> bool:
        return self.role == UserRole.CEO.value

    @property
    def is_member(self) -> bool:
        return self.role == UserRole.MEMBER.value

    @property
    def role_label(self) -> str:
        return "CEO" if self.is_ceo else "Member"

    @property
    def identifier_label(self) -> str:
        """The internal login identifier shown on profile pages."""
        return self.email or self.username

    @property
    def official_title(self) -> str:
        return self.title or self.role_label

    @property
    def initials(self) -> str:
        parts = [p for p in (self.full_name or self.username or "?").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    # --- password helpers -------------------------------------------------
    def set_password(self, raw_password: str) -> None:
        from app.services.crypto_service import hash_password

        self.password_hash = hash_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        from app.services.crypto_service import verify_password

        return verify_password(self.password_hash, raw_password)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<User {self.id} {self.username!r} {self.role}>"


class Invitation(TimestampMixin, db.Model):
    """Single-use, expiring invitation. The raw token is never stored."""

    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=UserRole.MEMBER.value)
    invited_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    invited_by = relationship("User", foreign_keys=[invited_by_id])
    accepted_user = relationship("User", foreign_keys=[accepted_user_id])

    @property
    def is_usable(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at is not None
            and self.expires_at > utcnow()
        )


class PasswordResetToken(TimestampMixin, db.Model):
    """Single-use password reset token; stored only as a hash."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user = relationship("User")

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at is not None and self.expires_at > utcnow()
