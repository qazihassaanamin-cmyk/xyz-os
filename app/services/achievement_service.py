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
    # --- The ten recognition achievements from the brief ------------------
    # Each is earned from a verified event and can be held once. Names, not
    # codes, are what members see, so they are written plainly.
    {
        "code": "first_delivery",
        "name": "First Delivery",
        "description": "Your first piece of work was accepted.",
        "rule_type": "COUNTER",
        "rule_field": "tasks_completed",
        "rule_threshold": 1,
        "points": 0,
        "icon": "task",
        "sort_order": 200,
    },
    {
        "code": "consistent_contributor",
        "name": "Consistent Contributor",
        "description": "A completed focus day in ten different working days.",
        "rule_type": "COUNTER",
        "rule_field": "focus_streak_days",
        "rule_threshold": 10,
        "points": 0,
        "icon": "streak",
        "sort_order": 210,
    },
    {
        "code": "quality_first",
        "name": "Quality First",
        "description": "Three deliveries accepted on the first submission.",
        "rule_type": "COUNTER",
        "rule_field": "first_pass_deliveries",
        "rule_threshold": 3,
        "points": 0,
        "icon": "check",
        "sort_order": 220,
    },
    {
        "code": "deadline_keeper",
        "name": "Deadline Keeper",
        "description": "Five accepted deliveries on or before their deadline.",
        "rule_type": "COUNTER",
        "rule_field": "tasks_on_time",
        "rule_threshold": 5,
        "points": 0,
        "icon": "clock",
        "sort_order": 230,
    },
    {
        "code": "problem_solver",
        "name": "Problem Solver",
        "description": "Resolved five blockers that were stopping other people's work.",
        "rule_type": "COUNTER",
        "rule_field": "blockers_resolved",
        "rule_threshold": 5,
        "points": 0,
        "icon": "blocker",
        "sort_order": 240,
    },
    {
        "code": "team_player",
        "name": "Team Player",
        "description": "Reviewed ten submissions from colleagues.",
        "rule_type": "COUNTER",
        "rule_field": "reviews_given",
        "rule_threshold": 10,
        "points": 0,
        "icon": "review",
        "sort_order": 250,
    },
    {
        "code": "knowledge_sharer",
        "name": "Knowledge Sharer",
        "description": "Published three knowledge entries for the team.",
        "rule_type": "COUNTER",
        "rule_field": "knowledge_entries",
        "rule_threshold": 3,
        "points": 0,
        "icon": "knowledge",
        "sort_order": 260,
    },
    {
        "code": "milestone_maker",
        "name": "Milestone Maker",
        "description": "Completed three project milestones.",
        "rule_type": "COUNTER",
        "rule_field": "milestones_completed",
        "rule_threshold": 3,
        "points": 0,
        "icon": "milestone",
        "sort_order": 270,
    },
    {
        "code": "reliability_award",
        "name": "Reliability Award",
        "description": "A check-in and a report on ten working days.",
        "rule_type": "COUNTER",
        "rule_field": "report_days",
        "rule_threshold": 10,
        "points": 0,
        "icon": "checkin",
        "sort_order": 280,
    },
    {
        "code": "project_finisher",
        "name": "Project Finisher",
        "description": "Was a member of a project taken through to completion.",
        "rule_type": "COUNTER",
        "rule_field": "projects_completed",
        "rule_threshold": 1,
        "points": 0,
        "icon": "project",
        "sort_order": 290,
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
            # Keep the catalogue definition current without touching an award
            # already made: awards store a name snapshot precisely so a later
            # edit here cannot rewrite someone's history.
            for field in ("name", "description", "rule_type", "rule_field", "rule_threshold", "points", "icon", "sort_order"):
                if field in spec and getattr(row, field) != spec[field]:
                    setattr(row, field, spec[field])
                    changed = True
    if changed:
        try:
            db.session.commit()
        except IntegrityError:  # pragma: no cover
            db.session.rollback()


def _counters_for(user: User) -> dict[str, int]:
    """Live counters, computed from stored records only.

    Nothing here is incremented by a view or by a client request: every value is
    derived from rows that a verified event already created.
    """
    from app.models.content import KnowledgeEntry, KnowledgeStatus
    from app.models.daily import DailyCheckin, DailyReport
    from app.models.focus import FocusSession, FocusStatus
    from app.models.project import Project, ProjectMember, ProjectStatus
    from app.models.task import Blocker, BlockerStatus, Review, ReviewDecision, Submission, SubmissionStatus
    from app.services.xp_service import ensure_profile

    profile = ensure_profile(user)

    checkins_total = DailyCheckin.query.filter_by(user_id=user.id).count()
    report_days = DailyReport.query.filter_by(user_id=user.id).count()

    # A day with both a check-in and a report is the reliability unit.
    checkin_days = {
        row.workday_date for row in DailyCheckin.query.filter_by(user_id=user.id).all()
    }
    report_days_set = {
        row.workday_date for row in DailyReport.query.filter_by(user_id=user.id).all()
    }
    paired_days = len(checkin_days & report_days_set)

    focus_days = {
        row.workday_date
        for row in FocusSession.query.filter_by(
            user_id=user.id, status=FocusStatus.COMPLETED.value
        ).all()
    }

    first_pass = Submission.query.filter(
        Submission.submitted_by_id == user.id,
        Submission.status == SubmissionStatus.APPROVED.value,
        Submission.revision_number == 1,
    ).count()

    tasks_on_time = Review.query.filter(
        Review.was_on_time.is_(True),
        Review.decision == ReviewDecision.APPROVED.value,
        Review.submission.has(Submission.submitted_by_id == user.id),
    ).count()

    reviews_given = Review.query.filter(Review.reviewer_id == user.id).count()
    blockers_resolved = Blocker.query.filter(
        Blocker.status == BlockerStatus.RESOLVED.value,
        Blocker.helper_id == user.id,
    ).count()
    knowledge_entries = KnowledgeEntry.query.filter(
        KnowledgeEntry.author_id == user.id,
        KnowledgeEntry.status == KnowledgeStatus.PUBLISHED.value,
    ).count()

    project_ids = [row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()]
    projects_completed = (
        Project.query.filter(
            Project.id.in_(project_ids),
            Project.status == ProjectStatus.COMPLETED.value,
        ).count()
        if project_ids
        else 0
    )

    return {
        "tasks_completed": profile.tasks_completed or 0,
        "tasks_on_time": tasks_on_time,
        "reviews_given": reviews_given,
        "reviews_approved": reviews_given,
        "blockers_resolved": blockers_resolved,
        "milestones_completed": profile.milestones_completed or 0,
        "focus_sessions_completed": profile.focus_sessions_completed or 0,
        "focus_streak_days": len(focus_days),
        "knowledge_entries": knowledge_entries,
        "first_pass_deliveries": first_pass,
        "report_days": paired_days if paired_days else report_days,
        "projects_completed": projects_completed,
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
