"""Achievements.

System achievements are earned from verified counters and granted by
``evaluate_all`` after any XP-earning event. Each award also grants a small XP
bonus through the normal ledger, so achievements cannot be farmed by replaying
the same event: the unique constraint on ``member_achievements`` blocks a second
award, and the XP row uses a matching idempotency key.

Recognition badges are *not* created here; those are granted by the CEO through
``badge_service`` and are kept visually and structurally separate.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.achievement import Achievement, AchievementKind, MemberAchievement
from app.models.base import utcnow
from app.models.user import User
from app.services.xp_service import award_xp, ensure_profile

log = logging.getLogger(__name__)

# The complete initial achievement catalogue. Thresholds are tuned so that the
# first achievements arrive within the first days of real use.
CATALOGUE: list[dict] = [
    {
        "code": "welcome_aboard",
        "name": "Welcome aboard",
        "description": "Account created and ready to contribute.",
        "rule_type": "IMMEDIATE",
        "points": 0,
        "icon": "welcome",
        "sort_order": 10,
    },
    {
        "code": "early_bird",
        "name": "First sign-in",
        "description": "Signed in to the Tavryc platform for the first time.",
        "rule_type": "IMMEDIATE",
        "points": 0,
        "icon": "welcome",
        "sort_order": 20,
    },
    {
        "code": "first_lockdown",
        "name": "Account secured",
        "description": "Set a personal password for the account.",
        "rule_type": "IMMEDIATE",
        "points": 0,
        "icon": "shield",
        "sort_order": 30,
    },
    {
        "code": "first_checkin",
        "name": "Checked in",
        "description": "Submitted a daily check-in.",
        "rule_type": "COUNTER",
        "rule_field": "checkins_total",
        "rule_threshold": 1,
        "points": 0,
        "icon": "checkin",
        "sort_order": 40,
    },
    {
        "code": "checkin_week",
        "name": "Seven check-ins",
        "description": "Submitted seven daily check-ins.",
        "rule_type": "COUNTER",
        "rule_field": "checkins_total",
        "rule_threshold": 7,
        "points": 20,
        "icon": "checkin",
        "sort_order": 50,
    },
    {
        "code": "first_focus",
        "name": "Deep work started",
        "description": "Completed a full focus session.",
        "rule_type": "COUNTER",
        "rule_field": "focus_sessions_completed",
        "rule_threshold": 1,
        "points": 0,
        "icon": "focus",
        "sort_order": 60,
    },
    {
        "code": "focus_ten",
        "name": "Ten focus sessions",
        "description": "Completed ten focus sessions.",
        "rule_type": "COUNTER",
        "rule_field": "focus_sessions_completed",
        "rule_threshold": 10,
        "points": 40,
        "icon": "focus",
        "sort_order": 70,
    },
    {
        "code": "focus_streak_five",
        "name": "Consistent week",
        "description": "Completed a focus session on five consecutive working days.",
        "rule_type": "STREAK",
        "rule_field": "focus_streak",
        "rule_threshold": 5,
        "points": 40,
        "icon": "streak",
        "sort_order": 80,
    },
    {
        "code": "first_task",
        "name": "First delivery",
        "description": "Completed and had a task accepted.",
        "rule_type": "COUNTER",
        "rule_field": "tasks_completed",
        "rule_threshold": 1,
        "points": 0,
        "icon": "task",
        "sort_order": 90,
    },
    {
        "code": "tasks_ten",
        "name": "Ten deliveries",
        "description": "Had ten tasks accepted.",
        "rule_type": "COUNTER",
        "rule_field": "tasks_completed",
        "rule_threshold": 10,
        "points": 60,
        "icon": "task",
        "sort_order": 100,
    },
    {
        "code": "tasks_twenty_five",
        "name": "Twenty-five deliveries",
        "description": "Had twenty-five tasks accepted.",
        "rule_type": "COUNTER",
        "rule_field": "tasks_completed",
        "rule_threshold": 25,
        "points": 120,
        "icon": "task",
        "sort_order": 110,
    },
    {
        "code": "on_time_five",
        "name": "Delivered on time",
        "description": "Delivered five accepted tasks on or before their deadline.",
        "rule_type": "COUNTER",
        "rule_field": "tasks_on_time",
        "rule_threshold": 5,
        "points": 40,
        "icon": "clock",
        "sort_order": 120,
    },
    {
        "code": "reviews_ten",
        "name": "Trusted reviewer",
        "description": "Completed ten reviews of team submissions.",
        "rule_type": "COUNTER",
        "rule_field": "reviews_approved",
        "rule_threshold": 10,
        "points": 60,
        "icon": "review",
        "sort_order": 130,
    },
    {
        "code": "unblocker",
        "name": "Unblocker",
        "description": "Resolved five blockers that were stopping work.",
        "rule_type": "COUNTER",
        "rule_field": "blockers_resolved",
        "rule_threshold": 5,
        "points": 60,
        "icon": "blocker",
        "sort_order": 140,
    },
    {
        "code": "milestone_first",
        "name": "Milestone reached",
        "description": "Completed a project milestone.",
        "rule_type": "COUNTER",
        "rule_field": "milestones_completed",
        "rule_threshold": 1,
        "points": 40,
        "icon": "milestone",
        "sort_order": 150,
    },
    {
        "code": "documenter",
        "name": "Documenter",
        "description": "Published five knowledge base entries.",
        "rule_type": "COUNTER",
        "rule_field": "knowledge_entries",
        "rule_threshold": 5,
        "points": 60,
        "icon": "knowledge",
        "sort_order": 160,
    },
    {
        "code": "level_five",
        "name": "Specialist",
        "description": "Reached level 5 through verified contribution.",
        "rule_type": "LEVEL",
        "rule_field": "current_level",
        "rule_threshold": 5,
        "points": 0,
        "icon": "level",
        "sort_order": 170,
    },
]


def ensure_catalogue() -> None:
    """Seed or repair the achievement catalogue. Idempotent."""
    existing = {row.code: row for row in Achievement.query.all()}
    changed = False
    for spec in CATALOGUE:
        row = existing.get(spec["code"])
        if row is None:
            db.session.add(
                Achievement(
                    code=spec["code"],
                    name=spec["name"],
                    description=spec["description"],
                    kind=AchievementKind.SYSTEM.value,
                    rule_type=spec.get("rule_type", "MANUAL"),
                    rule_threshold=spec.get("rule_threshold"),
                    rule_field=spec.get("rule_field"),
                    points=spec.get("points", 0),
                    icon=spec.get("icon", "achievement"),
                    sort_order=spec.get("sort_order", 0),
                    is_active=True,
                )
            )
            changed = True
        else:
            # Keep description text current without touching an award already made.
            if row.description != spec["description"]:
                row.description = spec["description"]
                changed = True
            if row.rule_threshold != spec.get("rule_threshold"):
                row.rule_threshold = spec.get("rule_threshold")
                changed = True
    if changed:
        try:
            db.session.commit()
        except IntegrityError:  # pragma: no cover
            db.session.rollback()


def _counters_for(user: User) -> dict[str, int]:
    """Live counters. Computed from stored records, never from client input."""
    from app.models.daily import DailyCheckin
    from app.services.xp_service import ensure_profile

    profile = ensure_profile(user)
    checkins_total = DailyCheckin.query.filter_by(user_id=user.id).count()

    # Review count is derived from the ledger rather than a separate table to
    # keep one source of truth for "reviewer" credit.
    from app.models.xp import XpReason, XpTransaction

    review_xp_count = (
        XpTransaction.query.filter_by(
            user_id=user.id, reason=XpReason.REVIEW_QUALITY_BONUS.value
        ).count()
    )

    return {
        "tasks_completed": profile.tasks_completed or 0,
        "tasks_on_time": profile.tasks_on_time or 0,
        "reviews_approved": max(profile.reviews_approved or 0, review_xp_count),
        "blockers_resolved": profile.blockers_resolved or 0,
        "milestones_completed": profile.milestones_completed or 0,
        "focus_sessions_completed": profile.focus_sessions_completed or 0,
        "knowledge_entries": profile.knowledge_entries or 0,
        "focus_streak": profile.focus_streak or 0,
        "report_streak": profile.report_streak or 0,
        "current_level": profile.current_level or 1,
        "checkins_total": checkins_total,
    }


def evaluate_all(user: User) -> list[MemberAchievement]:
    """Grant every achievement the member now qualifies for. Idempotent."""
    profile = ensure_profile(user)
    counters = _counters_for(user)
    granted: list[MemberAchievement] = []

    catalogue = (
        Achievement.query.filter_by(kind=AchievementKind.SYSTEM.value, is_active=True)
        .order_by(Achievement.sort_order.asc())
        .all()
    )
    owned = {row.achievement_id for row in MemberAchievement.query.filter_by(user_id=user.id).all()}

    for achievement in catalogue:
        if achievement.id in owned:
            continue
        if not _qualifies(achievement, counters):
            continue
        row = _grant_row(user, profile, achievement)
        if row is not None:
            granted.append(row)

    return granted


def _qualifies(achievement: Achievement, counters: dict[str, int]) -> bool:
    if achievement.rule_type in ("MANUAL", "IMMEDIATE"):
        # IMMEDIATE achievements are granted explicitly at the moment the event
        # happens (account creation, first sign-in), never swept up later.
        return False
    if achievement.rule_field is None or achievement.rule_threshold is None:
        return False
    value = counters.get(achievement.rule_field, 0)
    return value >= achievement.rule_threshold


def _grant_row(user, profile, achievement: Achievement) -> MemberAchievement | None:
    row = MemberAchievement(
        user_id=user.id,
        profile_id=profile.id,
        achievement_id=achievement.id,
        awarded_at=utcnow(),
        name_snapshot=achievement.name,
        points_awarded=achievement.points or 0,
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return None

    if achievement.points:
        from app.models.xp import XpReason

        transaction = award_xp(
            user,
            XpReason.MANUAL_AWARD.value,
            amount=achievement.points,
            note=f"Achievement unlocked: {achievement.name}.",
            source_type="achievement",
            source_id=achievement.id,
            idempotency_key=f"achievement:{achievement.code}:{user.id}",
            allow_manual=True,
        )
        if transaction is not None:
            row.xp_transaction_id = transaction.id
            db.session.commit()

    try:
        from app.services.notification_service import notify

        notify(
            user,
            "ACHIEVEMENT_EARNED",
            title=f"Achievement unlocked: {achievement.name}",
            body=achievement.description,
            link_endpoint="profile.detail",
            link_params=f"username={user.username}",
            subject_type="achievement",
            subject_id=achievement.id,
            dedupe_key=f"achievement:{achievement.code}:{user.id}",
        )
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Achievement notification failed for %s", user.id)

    return row


def grant(user: User, code: str) -> MemberAchievement | None:
    """Grant one achievement by code, if not already held."""
    achievement = Achievement.query.filter_by(code=code).first()
    if achievement is None:
        return None
    existing = MemberAchievement.query.filter_by(
        user_id=user.id, achievement_id=achievement.id
    ).first()
    if existing is not None:
        return existing
    profile = ensure_profile(user)
    return _grant_row(user, profile, achievement)


def for_user(user: User) -> list[MemberAchievement]:
    return (
        MemberAchievement.query.filter_by(user_id=user.id)
        .order_by(MemberAchievement.awarded_at.desc())
        .all()
    )


def recent(limit: int = 12):
    return (
        MemberAchievement.query.order_by(MemberAchievement.awarded_at.desc())
        .limit(limit)
        .all()
    )
