"""XP economy: profiles, the append-only ledger, levels and rule configuration.

Design rules enforced here:

* Every award is a row in ``xp_transactions``. Nothing is ever deleted; a
  correction is a new compensating row that points at the original.
* Every award carries an ``idempotency_key`` unique to the underlying event, so
  a retried request cannot double-credit the same work.
* ``XpProfile.balance`` and ``XpProfile.lifetime_xp`` are caches maintained
  inside the same transaction as the ledger row, and both are recomputable from
  the ledger if they ever drift.
"""

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


class XpReason(StrEnum):
    """Why XP was granted. Stored as a short code, rendered from a label map."""

    TASK_COMPLETED = "TASK_COMPLETED"
    REVIEW_QUALITY_BONUS = "REVIEW_QUALITY_BONUS"
    ON_TIME_BONUS = "ON_TIME_BONUS"
    MILESTONE_COMPLETED = "MILESTONE_COMPLETED"
    BLOCKER_RESOLVED = "BLOCKER_RESOLVED"
    BLOCKER_HELPED = "BLOCKER_HELPED"
    FOCUS_SESSION = "FOCUS_SESSION"
    CHECKIN_COMPLETED = "CHECKIN_COMPLETED"
    REPORT_COMPLETED = "REPORT_COMPLETED"
    SUGGESTION_IMPLEMENTED = "SUGGESTION_IMPLEMENTED"
    KNOWLEDGE_DOCUMENTED = "KNOWLEDGE_DOCUMENTED"
    ONBOARDING_HELP = "ONBOARDING_HELP"
    STREAK_BONUS = "STREAK_BONUS"
    WEEKLY_CHALLENGE = "WEEKLY_CHALLENGE"
    MANUAL_AWARD = "MANUAL_AWARD"
    MANUAL_DEDUCTION = "MANUAL_DEDUCTION"
    REVERSAL = "REVERSAL"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"


XP_REASON_LABELS: dict[str, str] = {
    XpReason.TASK_COMPLETED.value: "Task accepted",
    XpReason.REVIEW_QUALITY_BONUS.value: "Review quality bonus",
    XpReason.ON_TIME_BONUS.value: "Delivered before the deadline",
    XpReason.MILESTONE_COMPLETED.value: "Project milestone completed",
    XpReason.BLOCKER_RESOLVED.value: "Blocker resolved",
    XpReason.BLOCKER_HELPED.value: "Helped unblock a team member",
    XpReason.FOCUS_SESSION.value: "Completed focus session",
    XpReason.CHECKIN_COMPLETED.value: "Daily check-in submitted",
    XpReason.REPORT_COMPLETED.value: "Daily report submitted",
    XpReason.SUGGESTION_IMPLEMENTED.value: "Suggestion implemented",
    XpReason.KNOWLEDGE_DOCUMENTED.value: "Knowledge documented",
    XpReason.ONBOARDING_HELP.value: "Helped onboard a member",
    XpReason.STREAK_BONUS.value: "Consistency streak",
    XpReason.WEEKLY_CHALLENGE.value: "Weekly challenge completed",
    XpReason.MANUAL_AWARD.value: "Manual award",
    XpReason.MANUAL_DEDUCTION.value: "Manual deduction",
    XpReason.REVERSAL.value: "Reversal of an earlier award",
    XpReason.ADMIN_ADJUSTMENT.value: "Administrative adjustment",
}


class XpProfile(TimestampMixin, db.Model):
    """Cached gamification state for one member.

    Recomputable from ``xp_transactions``; the cache exists so dashboards do not
    aggregate the whole ledger on every request.
    """

    __tablename__ = "xp_profiles"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifetime_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    level_name: Mapped[str] = mapped_column(String(40), nullable=False, default="Initiate")
    xp_into_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    xp_to_next_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Consecutive working days with a completed focus session.
    focus_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    focus_streak_best: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    focus_streak_last_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Consecutive working days with both a check-in and a report.
    report_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_streak_best: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_streak_last_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Lifetime counters used by achievement rules so they never need a scan.
    tasks_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_on_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviews_approved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blockers_resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    milestones_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    focus_sessions_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    knowledge_entries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_award_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_decay_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="xp_profile")
    transactions = relationship(
        "XpTransaction",
        back_populates="profile",
        cascade="all, delete-orphan",
        order_by="XpTransaction.created_at.desc()",
    )
    achievements = relationship(
        "MemberAchievement",
        back_populates="profile",
        cascade="all, delete-orphan",
    )
    badges = relationship(
        "MemberBadge",
        back_populates="profile",
        cascade="all, delete-orphan",
    )

    # --- derived helpers --------------------------------------------------
    @property
    def progress_percent(self) -> int:
        """Progress through the current level, 0-100."""
        span = self.xp_to_next_level
        if span is None:
            return 100
        if span <= 0:
            return 100
        return max(0, min(100, round(100 * (self.xp_into_level or 0) / span)))

    @property
    def next_level_xp(self) -> int | None:
        if self.xp_to_next_level is None:
            return None
        return (self.lifetime_xp or 0) + self.xp_to_next_level

    def __repr__(self) -> str:  # pragma: no cover
        return f"<XpProfile user={self.user_id} level={self.current_level} xp={self.lifetime_xp}>"


class XpTransaction(TimestampMixin, db.Model):
    """Append-only XP ledger.

    ``amount`` is signed: positive for awards, negative for deductions and
    reversals. ``reversal_of_id`` links a correction to the row it cancels so
    the original award is never deleted.
    """

    __tablename__ = "xp_transactions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("xp_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Human-readable explanation. Mandatory for manual awards and deductions.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the award was for, so the profile history can link to it.
    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Unique per underlying event; the database rejects a duplicate award.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True, unique=True, index=True
    )
    # Who granted it. NULL for automatic awards produced by system rules.
    awarded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Set on the original row when a reversal has been issued against it.
    reversed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("xp_transactions.id", ondelete="SET NULL"), nullable=True
    )
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("xp_transactions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    balance_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set when a rule or a review decision was later found to be wrong.
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flagged_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    profile = relationship("XpProfile", back_populates="transactions")
    user = relationship("User", foreign_keys=[user_id])
    awarded_by = relationship("User", foreign_keys=[awarded_by_id])
    project = relationship("Project", foreign_keys=[project_id])
    task = relationship("Task", foreign_keys=[task_id])
    reversal_of = relationship(
        "XpTransaction", remote_side="XpTransaction.id", foreign_keys=[reversal_of_id]
    )

    __table_args__ = (
        Index("ix_xp_transactions_user_created", "user_id", "created_at"),
        Index("ix_xp_transactions_reason_created", "reason", "created_at"),
    )

    @property
    def reason_label(self) -> str:
        return XP_REASON_LABELS.get(self.reason, self.reason.replace("_", " ").title())

    @property
    def is_award(self) -> bool:
        return self.amount > 0

    @property
    def is_deduction(self) -> bool:
        return self.amount < 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<XpTransaction {self.id} user={self.user_id} {self.amount:+d} {self.reason}>"


class LevelDefinition(TimestampMixin, db.Model):
    """Level thresholds. Ordered by ``level``; seeded with a complete ladder."""

    __tablename__ = "level_definitions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    min_lifetime_xp: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(240), nullable=True)

    __table_args__ = (UniqueConstraint("level", name="uq_level_definitions_level"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LevelDefinition {self.level} {self.name} @{self.min_lifetime_xp}>"


class XpRule(TimestampMixin, db.Model):
    """Configurable point value for one award reason.

    Seeded with complete, usable values so the system works from first launch.
    The CEO may adjust them later; a disabled rule stops awarding but never
    removes XP already granted.
    """

    __tablename__ = "xp_rules"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String(400), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Daily ceiling per member, to stop low-value activity farming. NULL = none.
    daily_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Minimum seconds between two awards of this rule for the same member.
    cooldown_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Difficulty multiplier applied to a task's base XP, in percent.
    multiplier_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<XpRule {self.code} {self.points}>"


class Setting(TimestampMixin, db.Model):
    """Key/value configuration the CEO can change without a deployment."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, default="str")
    label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    description: Mapped[str | None] = mapped_column(String(400), nullable=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="general", index=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @staticmethod
    def get(key: str, default=None):
        row = Setting.query.filter_by(key=key).first()
        if row is None or row.value is None:
            return default
        return row.cast_value()

    def cast_value(self):
        if self.value is None:
            return None
        if self.value_type == "int":
            try:
                return int(self.value)
            except (TypeError, ValueError):
                return None
        if self.value_type == "bool":
            return str(self.value).strip().lower() in {"1", "true", "yes", "on"}
        if self.value_type == "float":
            try:
                return float(self.value)
            except (TypeError, ValueError):
                return None
        return self.value


class Streak(TimestampMixin, db.Model):
    """A per-member streak, stored per kind.

    Streaks count *working days* on which a verified action happened. Approved
    leave and company holidays neither extend nor break a run, so nobody is
    punished for taking agreed time off. The kind is one of ``focus``,
    ``report`` or ``delivery``.
    """

    __tablename__ = "streaks"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    current_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The most recent working day that counted toward the run.
    last_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # The day the current run began, used to make the bonus idempotent per run.
    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    broken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_streak_member_kind"),
        Index("ix_streaks_kind_current", "kind", "current_count"),
    )

    @property
    def is_active(self) -> bool:
        return self.current_count > 0 and self.broken_at is None


class UserXpSummary(TimestampMixin, db.Model):
    """A materialised view of one member's XP, rebuilt from the ledger.

    ``xp_transactions`` is the single source of truth; this table exists so a
    dashboard can read a member's totals in one row instead of aggregating the
    whole ledger on every request. It is rebuilt by ``flask xp-reconcile`` and
    after any correction, and the reconcile command asserts that the two agree.
    """

    __tablename__ = "user_xp_summary"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifetime_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    awarded_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deducted_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reversed_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    level_name: Mapped[str] = mapped_column(String(40), nullable=False, default="Initiate")
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_award_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_award_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this row was last rebuilt, so staleness is visible rather than guessed.
    rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserXpSummary user={self.user_id} lifetime={self.lifetime_xp}>"
