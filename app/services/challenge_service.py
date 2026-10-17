"""Weekly challenges.

A challenge is defined for a week and a metric. Progress is incremented from
verified events only, and the reward is granted once through the normal XP
ledger with an idempotency key tied to the challenge and the member.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import utcnow
from app.models.challenge import (
    Challenge,
    ChallengeMetric,
    ChallengeProgress,
)
from app.services.workday_service import week_bounds

log = logging.getLogger(__name__)

# Challenge templates rotated week by week so the programme does not go stale.
# Every metric here is derived from verified events, so a challenge cannot be
# completed by logging in or by running a timer.
TEMPLATES: list[dict] = [
    {
        "key": "delivery_week",
        "title": "Delivery week",
        "description": "Have three tasks accepted this week.",
        "metric": ChallengeMetric.TASKS_COMPLETED.value,
        "target_value": 3,
        "reward_xp": 60,
    },
    {
        "key": "focus_week",
        "title": "Sustained focus",
        "description": "Complete four focus sessions this week.",
        "metric": ChallengeMetric.FOCUS_SESSIONS.value,
        "target_value": 4,
        "reward_xp": 60,
    },
    {
        "key": "punctual_week",
        "title": "Punctual delivery",
        "description": "Deliver two accepted tasks on or before their deadline.",
        "metric": ChallengeMetric.ON_TIME_DELIVERIES.value,
        "target_value": 2,
        "reward_xp": 60,
    },
    {
        "key": "review_week",
        "title": "Keep the queue moving",
        "description": "Complete three reviews of team submissions.",
        "metric": ChallengeMetric.REVIEWS_COMPLETED.value,
        "target_value": 3,
        "reward_xp": 60,
    },
]


def _current_week(day: date | None = None) -> tuple[date, date]:
    return week_bounds(day)


def ensure_weekly_challenges(day: date | None = None) -> list[Challenge]:
    """Create this week's company-wide challenges if they do not exist."""
    week_start, week_end = _current_week(day)
    created: list[Challenge] = []
    for template in TEMPLATES:
        existing = (
            Challenge.query.filter_by(
                week_start=week_start, title=template["title"], user_id=None
            ).first()
        )
        if existing is not None:
            created.append(existing)
            continue
        challenge = Challenge(
            title=template["title"],
            description=template["description"],
            metric=template["metric"],
            target_value=template["target_value"],
            reward_xp=template["reward_xp"],
            week_start=week_start,
            week_end=week_end,
            user_id=None,
            is_active=True,
        )
        db.session.add(challenge)
        created.append(challenge)
    try:
        db.session.commit()
    except IntegrityError:  # pragma: no cover
        db.session.rollback()
    return created


def active_challenges(day: date | None = None) -> list[Challenge]:
    week_start, week_end = _current_week(day)
    return (
        Challenge.query.filter(
            Challenge.is_active.is_(True),
            Challenge.week_start == week_start,
            Challenge.week_end == week_end,
        )
        .order_by(Challenge.id.asc())
        .all()
    )


def progress_for(challenge: Challenge, user) -> ChallengeProgress:
    row = ChallengeProgress.query.filter_by(
        challenge_id=challenge.id, user_id=user.id
    ).first()
    if row is None:
        row = ChallengeProgress(challenge_id=challenge.id, user_id=user.id, current_value=0)
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:  # pragma: no cover
            db.session.rollback()
            row = ChallengeProgress.query.filter_by(
                challenge_id=challenge.id, user_id=user.id
            ).first()
    return row


def _metric_value(challenge: Challenge, user) -> int:
    """Count the verified events behind a challenge metric for this week."""
    from app.models.daily import DailyCheckin
    from app.models.focus import FocusSession, FocusStatus
    from app.models.project import Milestone
    from app.models.task import Review, Submission, SubmissionStatus
    from app.services.xp_service import ensure_profile

    start, end = challenge.week_start, challenge.week_end
    metric = challenge.metric

    if metric == ChallengeMetric.TASKS_COMPLETED.value:
        return (
            Submission.query.filter(
                Submission.submitted_by_id == user.id,
                Submission.status == SubmissionStatus.APPROVED.value,
                db.func.date(Submission.updated_at) >= start,
                db.func.date(Submission.updated_at) <= end,
            ).count()
        )
    if metric == ChallengeMetric.ON_TIME_DELIVERIES.value:
        return (
            Submission.query.filter(
                Submission.submitted_by_id == user.id,
                Submission.status == SubmissionStatus.APPROVED.value,
                Submission.due_date_snapshot.isnot(None),
                db.func.date(Submission.updated_at) <= Submission.due_date_snapshot,
                db.func.date(Submission.updated_at) >= start,
                db.func.date(Submission.updated_at) <= end,
            ).count()
        )
    if metric == ChallengeMetric.FOCUS_SESSIONS.value:
        return (
            FocusSession.query.filter(
                FocusSession.user_id == user.id,
                FocusSession.status == FocusStatus.COMPLETED.value,
                FocusSession.workday_date >= start,
                FocusSession.workday_date <= end,
            ).count()
        )
    if metric == ChallengeMetric.CHECKIN_STREAK.value:
        return (
            DailyCheckin.query.filter(
                DailyCheckin.user_id == user.id,
                DailyCheckin.workday_date >= start,
                DailyCheckin.workday_date <= end,
            ).count()
        )
    if metric == ChallengeMetric.REVIEWS_COMPLETED.value:
        return (
            Review.query.filter(
                Review.reviewer_id == user.id,
                db.func.date(Review.decided_at) >= start,
                db.func.date(Review.decided_at) <= end,
            ).count()
        )
    if metric == ChallengeMetric.BLOCKERS_RESOLVED.value:
        from app.models.task import Blocker, BlockerStatus

        return (
            Blocker.query.filter(
                Blocker.status == BlockerStatus.RESOLVED.value,
                Blocker.resolved_by_id == user.id,
                db.func.date(Blocker.resolved_at) >= start,
                db.func.date(Blocker.resolved_at) <= end,
            ).count()
        )
    if metric == ChallengeMetric.SUGGESTIONS_IMPLEMENTED.value:
        from app.models.suggestion import Suggestion, SuggestionStatus

        return (
            Suggestion.query.filter(
                Suggestion.author_id == user.id,
                Suggestion.status == SuggestionStatus.IMPLEMENTED.value,
                db.func.date(Suggestion.implemented_at) >= start,
                db.func.date(Suggestion.implemented_at) <= end,
            ).count()
        )
    if metric == ChallengeMetric.KNOWLEDGE_ENTRIES.value:
        from app.models.content import KnowledgeEntry, KnowledgeStatus

        return (
            KnowledgeEntry.query.filter(
                KnowledgeEntry.author_id == user.id,
                KnowledgeEntry.status == KnowledgeStatus.PUBLISHED.value,
                db.func.date(KnowledgeEntry.published_at) >= start,
                db.func.date(KnowledgeEntry.published_at) <= end,
            ).count()
        )
    _ = (Milestone, ensure_profile)
    return 0


def evaluate_challenges(user) -> list[ChallengeProgress]:
    """Recompute progress against active challenges and grant rewards."""
    from app.models.xp import XpReason
    from app.services.xp_service import award_xp

    completed: list[ChallengeProgress] = []
    for challenge in active_challenges():
        if challenge.user_id is not None and challenge.user_id != user.id:
            continue
        value = _metric_value(challenge, user)
        row = progress_for(challenge, user)
        if value != row.current_value:
            row.current_value = value
            db.session.commit()

        if row.completed_at is not None:
            continue
        if row.current_value < challenge.target_value:
            continue

        row.completed_at = utcnow()
        db.session.commit()

        if challenge.reward_xp:
            award_xp(
                user,
                XpReason.WEEKLY_CHALLENGE.value,
                amount=challenge.reward_xp,
                note=f"Weekly challenge completed: {challenge.title}.",
                source_type="challenge",
                source_id=challenge.id,
                idempotency_key=f"challenge:{challenge.id}:{user.id}",
            )
            row.xp_awarded_at = utcnow()
            db.session.commit()

        try:
            from app.services.notification_service import notify

            notify(
                user,
                "ACHIEVEMENT_EARNED",
                title=f"Challenge complete: {challenge.title}",
                body=f"+{challenge.reward_xp} XP — {challenge.description or ''}".strip(),
                link_endpoint="xp.index",
                subject_type="challenge",
                subject_id=challenge.id,
                dedupe_key=f"challenge:{challenge.id}:{user.id}",
            )
        except Exception:  # pragma: no cover
            db.session.rollback()
            log.exception("Challenge notification failed for %s", user.id)

        completed.append(row)

    return completed


def summary_for(user) -> list[dict]:
    """Challenge rows shaped for a template."""
    rows = []
    for challenge in active_challenges():
        if challenge.user_id is not None and challenge.user_id != user.id:
            continue
        progress = progress_for(challenge, user)
        rows.append(
            {
                "challenge": challenge,
                "progress": progress,
                "percent": progress.percent,
                "is_complete": progress.is_complete,
            }
        )
    return rows
