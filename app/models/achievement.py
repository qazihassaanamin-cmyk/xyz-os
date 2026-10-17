"""Achievements (system-awarded), badges and titles (CEO-awarded).

System achievements are earned from verified events and can never be granted by
hand. Custom badges and recognition titles are granted by the CEO and are always
labelled as recognition, never as a formal company appointment.
"""

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


class AchievementKind(StrEnum):
    SYSTEM = "SYSTEM"
    RECOGNITION = "RECOGNITION"


class Achievement(TimestampMixin, db.Model):
    """Catalogue entry. ``code`` is stable and referenced by award rules."""

    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AchievementKind.SYSTEM.value, index=True
    )
    # Machine-readable rule evaluated by achievement_service.
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False, default="MANUAL")
    rule_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_field: Mapped[str | None] = mapped_column(String(60), nullable=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    icon: Mapped[str] = mapped_column(String(40), nullable=False, default="achievement")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    awards = relationship("MemberAchievement", back_populates="achievement")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Achievement {self.code!r}>"


class MemberAchievement(TimestampMixin, db.Model):
    """One member earning one achievement. Unique per member and achievement."""

    __tablename__ = "member_achievements"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("xp_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    achievement_id: Mapped[int] = mapped_column(
        ForeignKey("achievements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # Snapshot so the profile history still reads correctly if the catalogue text
    # is edited later.
    name_snapshot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    points_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    xp_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("xp_transactions.id", ondelete="SET NULL"), nullable=True
    )
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="achievements")
    profile = relationship("XpProfile", back_populates="achievements")
    achievement = relationship("Achievement", back_populates="awards")

    __table_args__ = (
        UniqueConstraint("user_id", "achievement_id", name="uq_member_achievement"),
        Index("ix_member_achievements_user_awarded", "user_id", "awarded_at"),
    )

    @property
    def display_name(self) -> str:
        return self.name_snapshot or (self.achievement.name if self.achievement else "Achievement")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MemberAchievement user={self.user_id} achievement={self.achievement_id}>"


class MemberBadge(TimestampMixin, db.Model):
    """A recognition title or badge granted by the CEO to one member.

    ``achievement_id`` is set when the badge was spawned from a catalogue entry;
    it stays NULL for free-form recognition, which keeps the two concepts
    distinct as required.
    """

    __tablename__ = "member_badges"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("xp_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    achievement_id: Mapped[int | None] = mapped_column(
        ForeignKey("achievements.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    awarded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # Optional link to the project or milestone the recognition relates to.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    milestone_id: Mapped[int | None] = mapped_column(
        ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True
    )
    # Shown on the profile as active recognition; revoked badges are kept for
    # the audit trail but hidden from the active list.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form badge text is decoration; it never implies an appointment.
    is_formal_appointment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user = relationship("User", foreign_keys=[user_id])
    profile = relationship("XpProfile", back_populates="badges")
    achievement = relationship("Achievement")
    awarded_by = relationship("User", foreign_keys=[awarded_by_id])
    project = relationship("Project", foreign_keys=[project_id])
    milestone = relationship("Milestone", foreign_keys=[milestone_id])

    __table_args__ = (
        Index("ix_member_badges_user_active", "user_id", "is_active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MemberBadge {self.name!r} user={self.user_id} active={self.is_active}>"
