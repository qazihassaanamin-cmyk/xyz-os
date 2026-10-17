"""A member's unified contribution history.

Built by reading the stored records — tasks, reviews, achievements, badges,
focus sessions and recognised announcements — rather than maintaining a second
event log that could drift from the truth. Every entry carries a link target
that the caller must still authorize.
"""

from __future__ import annotations

from datetime import timedelta

from app.extensions import db
from app.models.achievement import MemberAchievement, MemberBadge
from app.models.base import utcnow
from app.models.content import Announcement
from app.models.focus import FocusSession, FocusStatus
from app.models.task import Review, Submission, SubmissionStatus, Task
from app.utils.dates import company_today, humanise_delta


def _entry(kind, title, *, when, detail=None, url_endpoint=None, url_params=None, tone="neutral"):
    return {
        "kind": kind,
        "title": title,
        "detail": detail,
        "when": when,
        "relative": humanise_delta(when),
        "url_endpoint": url_endpoint,
        "url_params": url_params,
        "tone": tone,
    }


def contribution_history(user, *, days: int = 120, limit: int = 80) -> list[dict]:
    """Chronological record of verified contribution, newest first."""
    since = utcnow() - timedelta(days=days)
    entries: list[dict] = []

    # --- accepted submissions -------------------------------------------------
    submissions = (
        Submission.query.filter(
            Submission.submitted_by_id == user.id, Submission.created_at >= since
        )
        .order_by(Submission.created_at.desc())
        .limit(limit)
        .all()
    )
    for submission in submissions:
        task = submission.task
        if task is None:
            continue
        if submission.status == SubmissionStatus.APPROVED.value:
            entries.append(
                _entry(
                    "submission_approved",
                    f"Work accepted: {task.title}",
                    when=submission.created_at,
                    detail=(submission.summary or "")[:200],
                    url_endpoint="tasks.detail",
                    url_params={"task_id": task.id},
                    tone="success",
                )
            )
        elif submission.status == SubmissionStatus.CHANGES_REQUESTED.value:
            latest = submission.latest_review
            entries.append(
                _entry(
                    "submission_returned",
                    f"Returned for changes: {task.title}",
                    when=submission.created_at,
                    detail=(latest.feedback if latest else None) or None,
                    url_endpoint="tasks.detail",
                    url_params={"task_id": task.id},
                    tone="attention",
                )
            )
        else:
            entries.append(
                _entry(
                    "submission_made",
                    f"Work submitted: {task.title}",
                    when=submission.created_at,
                    detail=f"Revision {submission.revision_number}",
                    url_endpoint="tasks.detail",
                    url_params={"task_id": task.id},
                )
            )

    # --- reviews performed ----------------------------------------------------
    reviews = (
        Review.query.filter(Review.reviewer_id == user.id, Review.decided_at >= since)
        .order_by(Review.decided_at.desc())
        .limit(limit)
        .all()
    )
    for review in reviews:
        submission = review.submission
        task = submission.task if submission else None
        if task is None:
            continue
        entries.append(
            _entry(
                "review_given",
                f"Reviewed: {task.title}",
                when=review.decided_at,
                detail="Approved" if review.decision == "APPROVED" else "Requested changes",
                url_endpoint="tasks.detail",
                url_params={"task_id": task.id},
            )
        )

    # --- completed tasks ------------------------------------------------------
    tasks = (
        Task.query.filter(
            Task.assignee_id == user.id,
            Task.status == "COMPLETED",
            Task.completed_at.isnot(None),
            Task.completed_at >= since,
        )
        .order_by(Task.completed_at.desc())
        .limit(limit)
        .all()
    )
    for task in tasks:
        entries.append(
            _entry(
                "task_completed",
                f"Task completed: {task.title}",
                when=task.completed_at,
                detail=task.project.name if task.project else None,
                url_endpoint="tasks.detail",
                url_params={"task_id": task.id},
                tone="success",
            )
        )

    # --- achievements ---------------------------------------------------------
    achievements = (
        MemberAchievement.query.filter(
            MemberAchievement.user_id == user.id, MemberAchievement.awarded_at >= since
        )
        .order_by(MemberAchievement.awarded_at.desc())
        .limit(limit)
        .all()
    )
    for row in achievements:
        entries.append(
            _entry(
                "achievement",
                f"Achievement: {row.display_name}",
                when=row.awarded_at,
                detail=row.achievement.description if row.achievement else None,
                url_endpoint="profile.detail",
                url_params={"username": user.username, "tab": "achievements"},
                tone="milestone",
            )
        )

    # --- recognition badges ---------------------------------------------------
    badges = (
        MemberBadge.query.filter(
            MemberBadge.user_id == user.id, MemberBadge.awarded_at >= since
        )
        .order_by(MemberBadge.awarded_at.desc())
        .limit(limit)
        .all()
    )
    for badge in badges:
        entries.append(
            _entry(
                "badge",
                f"Recognition: {badge.name}",
                when=badge.awarded_at,
                detail=badge.description,
                url_endpoint="profile.detail",
                url_params={"username": user.username, "tab": "recognition"},
                tone="milestone",
            )
        )

    # --- focus sessions -------------------------------------------------------
    sessions = (
        FocusSession.query.filter(
            FocusSession.user_id == user.id,
            FocusSession.status == FocusStatus.COMPLETED.value,
            FocusSession.started_at >= since,
        )
        .order_by(FocusSession.started_at.desc())
        .limit(limit)
        .all()
    )
    for session in sessions:
        entries.append(
            _entry(
                "focus",
                "Focus session completed",
                when=session.started_at,
                detail=f"{int(session.duration_seconds or 0) // 60} minutes"
                + (f" · {session.task.title}" if session.task else ""),
                url_endpoint="focus.history",
                tone="neutral",
            )
        )

    # --- recognition announcements -------------------------------------------
    announcements = (
        Announcement.query.filter(
            Announcement.is_recognition.is_(True), Announcement.created_at >= since
        )
        .order_by(Announcement.created_at.desc())
        .limit(limit)
        .all()
    )
    for announcement in announcements:
        if user.display_name not in (announcement.body or ""):
            continue
        entries.append(
            _entry(
                "recognition",
                announcement.title,
                when=announcement.created_at,
                detail=(announcement.body or "")[:200],
                url_endpoint="announcements.detail",
                url_params={"announcement_id": announcement.id},
                tone="milestone",
            )
        )

    entries.sort(key=lambda item: item["when"] or utcnow(), reverse=True)
    return entries[:limit]


def weekly_progress(user) -> dict:
    """This week's verified activity, for the profile and dashboard."""
    from app.services.workday_service import week_bounds

    today = company_today()
    week_start, week_end = week_bounds(today)

    approved = Submission.query.filter(
        Submission.submitted_by_id == user.id,
        Submission.status == SubmissionStatus.APPROVED.value,
        db.func.date(Submission.updated_at) >= week_start,
        db.func.date(Submission.updated_at) <= week_end,
    ).count()

    focus_sessions = FocusSession.query.filter(
        FocusSession.user_id == user.id,
        FocusSession.status == FocusStatus.COMPLETED.value,
        FocusSession.workday_date >= week_start,
        FocusSession.workday_date <= week_end,
    ).count()

    reviews_done = Review.query.filter(
        Review.reviewer_id == user.id,
        db.func.date(Review.decided_at) >= week_start,
        db.func.date(Review.decided_at) <= week_end,
    ).count()

    from app.models.xp import XpTransaction

    xp_earned = (
        db.session.query(db.func.coalesce(db.func.sum(XpTransaction.amount), 0))
        .filter(
            XpTransaction.user_id == user.id,
            XpTransaction.amount > 0,
            db.func.date(XpTransaction.created_at) >= week_start,
            db.func.date(XpTransaction.created_at) <= week_end,
        )
        .scalar()
    )

    return {
        "week_start": week_start,
        "week_end": week_end,
        "tasks_approved": approved,
        "focus_sessions": focus_sessions,
        "reviews_done": reviews_done,
        "xp_earned": int(xp_earned or 0),
    }


def quality_indicators(user, *, days: int = 90) -> dict:
    """Rework ratio and on-time ratio, computed from reviews only."""
    since = utcnow() - timedelta(days=days)

    approved = Submission.query.filter(
        Submission.submitted_by_id == user.id,
        Submission.status == SubmissionStatus.APPROVED.value,
        Submission.created_at >= since,
    ).count()

    total_reviewed = (
        Submission.query.filter(
            Submission.submitted_by_id == user.id,
            Submission.status.in_(
                [SubmissionStatus.APPROVED.value, SubmissionStatus.CHANGES_REQUESTED.value]
            ),
            Submission.created_at >= since,
        ).count()
    )

    on_time = Review.query.filter(
        Review.reviewer_id.isnot(None),
        Review.decided_at >= since,
        Review.was_on_time.is_(True),
        Review.submission.has(Submission.submitted_by_id == user.id),
    ).count()

    first_pass = (
        Submission.query.filter(
            Submission.submitted_by_id == user.id,
            Submission.status == SubmissionStatus.APPROVED.value,
            Submission.revision_number == 1,
            Submission.created_at >= since,
        ).count()
    )

    return {
        "approved": approved,
        "reviewed": total_reviewed,
        "on_time": on_time,
        "first_pass": first_pass,
        "rework_percent": (
            round(100 * (total_reviewed - first_pass) / total_reviewed)
            if total_reviewed
            else 0
        ),
        "on_time_percent": round(100 * on_time / approved) if approved else 0,
        "window_days": days,
    }
