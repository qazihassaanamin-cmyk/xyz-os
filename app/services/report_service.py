"""Reports and metrics.

Every metric is returned as its own value with the query that produced it, so
the CEO dashboard can show task completion, review outcomes, focus compliance
and reporting compliance as separate figures. They are never combined into a
single performance score, because doing so would misrepresent what each
measures.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.extensions import db
from app.models.audit import AuditLog
from app.models.daily import DailyCheckin, DailyReport
from app.models.focus import FocusSession, FocusStatus
from app.models.project import Milestone, MilestoneStatus, Project, ProjectMember
from app.models.task import Blocker, BlockerStatus, Review, Submission, SubmissionStatus, Task, TaskStatus
from app.models.user import User
from app.utils.dates import company_today
from app.services.workday_service import is_working_day_for, week_bounds


def default_range(*, days: int = 30) -> tuple[date, date]:
    today = company_today()
    return today - timedelta(days=days - 1), today


def parse_range(start: date | None, end: date | None, *, default_days: int = 30) -> tuple[date, date]:
    today = company_today()
    if start is None and end is None:
        return default_range(days=default_days)
    if start is None:
        start = (end or today) - timedelta(days=default_days - 1)
    if end is None:
        end = today
    if end < start:
        start, end = end, start
    return start, end


def _window_bounds(start: date, end: date):
    from app.utils.dates import day_bounds_utc

    return day_bounds_utc(start)[0], day_bounds_utc(end)[1]


# ---------------------------------------------------------------------------
# Company-wide metrics, each independent
# ---------------------------------------------------------------------------

def task_metrics(*, start: date, end: date, project: Project | None = None, member: User | None = None) -> dict:
    """Delivery metrics. Each number links back to the underlying records."""
    start_dt, end_dt = _window_bounds(start, end)

    base = Task.query
    if project is not None:
        base = base.filter(Task.project_id == project.id)
    if member is not None:
        base = base.filter(Task.assignee_id == member.id)

    created = base.filter(Task.created_at >= start_dt, Task.created_at <= end_dt).count()

    completed_query = base.filter(
        Task.status == TaskStatus.COMPLETED.value,
        Task.completed_at.isnot(None),
        Task.completed_at >= start_dt,
        Task.completed_at <= end_dt,
    )
    completed = completed_query.count()

    open_query = base.filter(
        Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value])
    )
    open_count = open_query.count()

    overdue_query = open_query.filter(
        Task.due_date.isnot(None), Task.due_date < company_today()
    )

    blocked_query = open_query.filter(Task.status == TaskStatus.BLOCKED.value)
    in_review_query = base.filter(Task.status == TaskStatus.IN_REVIEW.value)

    return {
        "created": created,
        "completed": completed,
        "open": open_count,
        "overdue": overdue_query.count(),
        "blocked": blocked_query.count(),
        "awaiting_review": in_review_query.count(),
        "completion_rate": round(100 * completed / created) if created else 0,
    }


def review_metrics(*, start: date, end: date, project: Project | None = None, member: User | None = None) -> dict:
    """Deliverable approval metrics, kept separate from task counts."""
    start_dt, end_dt = _window_bounds(start, end)

    query = Review.query.filter(Review.decided_at >= start_dt, Review.decided_at <= end_dt)
    if member is not None:
        query = query.filter(Review.submission.has(Submission.submitted_by_id == member.id))
    if project is not None:
        query = query.filter(
            Review.submission.has(Submission.task.has(Task.project_id == project.id))
        )

    approved = query.filter(Review.decision == "APPROVED").count()
    changes = query.filter(Review.decision == "CHANGES_REQUESTED").count()
    on_time = query.filter(Review.was_on_time.is_(True)).count()

    pending_query = Submission.query.filter(Submission.status == SubmissionStatus.PENDING.value)
    if member is not None:
        pending_query = pending_query.filter(Submission.submitted_by_id == member.id)
    if project is not None:
        pending_query = pending_query.filter(Submission.task.has(Task.project_id == project.id))

    total = approved + changes
    return {
        "approved": approved,
        "changes_requested": changes,
        "on_time": on_time,
        "awaiting_review": pending_query.count(),
        "first_pass_rate": round(100 * approved / total) if total else 0,
    }


def focus_metrics(*, start: date, end: date, member: User | None = None) -> dict:
    """Focus compliance. Explicitly not a measure of attentiveness."""
    start_dt, end_dt = _window_bounds(start, end)

    query = FocusSession.query.filter(
        FocusSession.workday_date >= start, FocusSession.workday_date <= end
    )
    if member is not None:
        query = query.filter(FocusSession.user_id == member.id)

    completed = query.filter(FocusSession.status == FocusStatus.COMPLETED.value).count()
    interrupted = query.filter(FocusSession.status == FocusStatus.INTERRUPTED.value).count()
    abandoned = query.filter(FocusSession.status == FocusStatus.ABANDONED.value).count()
    credited_days = query.filter(
        FocusSession.status == FocusStatus.COMPLETED.value,
        FocusSession.credited.is_(True),
    ).count()

    minutes = (
        db.session.query(db.func.coalesce(db.func.sum(FocusSession.duration_seconds), 0))
        .filter(
            FocusSession.workday_date >= start,
            FocusSession.workday_date <= end,
            FocusSession.status == FocusStatus.COMPLETED.value,
            *( [FocusSession.user_id == member.id] if member is not None else [] ),
        )
        .scalar()
    )

    _ = (start_dt, end_dt)
    return {
        "completed_sessions": completed,
        "interrupted": interrupted,
        "abandoned": abandoned,
        "credited_days": credited_days,
        "total_minutes": int(minutes or 0) // 60,
    }


def reporting_metrics(*, start: date, end: date, member: User | None = None) -> dict:
    """Daily accountability compliance."""
    checkins = DailyCheckin.query.filter(
        DailyCheckin.workday_date >= start, DailyCheckin.workday_date <= end
    )
    reports = DailyReport.query.filter(
        DailyReport.workday_date >= start, DailyReport.workday_date <= end
    )
    if member is not None:
        checkins = checkins.filter(DailyCheckin.user_id == member.id)
        reports = reports.filter(DailyReport.user_id == member.id)

    return {
        "checkins": checkins.count(),
        "reports": reports.count(),
    }


def compliance_by_member(*, start: date, end: date, members=None) -> list[dict]:
    """Per-member rows with each metric kept in its own column."""
    members = members or User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()
    rows = []
    for user in members:
        working_days = 0
        cursor = start
        while cursor <= end:
            if is_working_day_for(user, cursor):
                working_days += 1
            cursor += timedelta(days=1)

        tasks = task_metrics(start=start, end=end, member=user)
        reviews = review_metrics(start=start, end=end, member=user)
        focus = focus_metrics(start=start, end=end, member=user)
        reporting = reporting_metrics(start=start, end=end, member=user)

        rows.append(
            {
                "user": user,
                "working_days": working_days,
                "tasks_completed": tasks["completed"],
                "_overdue": tasks["overdue"],
                "open_tasks": tasks["open"],
                "awaiting_review": reviews["awaiting_review"],
                "approved": reviews["approved"],
                "changes_requested": reviews["changes_requested"],
                "focus_days": focus["credited_days"],
                "focus_minutes": focus["total_minutes"],
                "checkins": reporting["checkins"],
                "reports": reporting["reports"],
                "checkin_rate": round(100 * reporting["checkins"] / working_days) if working_days else 0,
                "report_rate": round(100 * reporting["reports"] / working_days) if working_days else 0,
                "focus_rate": round(100 * focus["credited_days"] / working_days) if working_days else 0,
            }
        )
    return rows


def project_summary(*, project: Project) -> dict:
    tasks = task_metrics(start=date(2000, 1, 1), end=company_today(), project=project)
    reviews = review_metrics(start=date(2000, 1, 1), end=company_today(), project=project)
    milestones = Milestone.query.filter_by(project_id=project.id).all()
    return {
        "tasks": tasks,
        "reviews": reviews,
        "milestones_total": len(milestones),
        "milestones_done": sum(1 for m in milestones if m.status == MilestoneStatus.COMPLETED.value),
        "milestones_overdue": sum(1 for m in milestones if m.is_overdue),
        "members": len(project.members),
    }


# ---------------------------------------------------------------------------
# CEO attention queue
# ---------------------------------------------------------------------------

def attention_queue(*, limit: int = 40) -> dict:
    """What genuinely needs the CEO's attention, ordered by urgency."""
    today = company_today()

    overdue = (
        Task.query.filter(
            Task.status.notin_(
                [TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value, TaskStatus.BACKLOG.value]
            ),
            Task.due_date.isnot(None),
            Task.due_date < today,
        )
        .order_by(Task.due_date.asc())
        .limit(limit)
        .all()
    )

    awaiting_review = (
        Submission.query.filter(Submission.status == SubmissionStatus.PENDING.value)
        .order_by(Submission.created_at.asc())
        .limit(limit)
        .all()
    )

    open_blockers = (
        Blocker.query.filter(Blocker.status.in_([BlockerStatus.OPEN.value, BlockerStatus.IN_PROGRESS.value]))
        .order_by(Blocker.created_at.asc())
        .limit(limit)
        .all()
    )

    # Members needing help: blocked tasks they own, or unanswered check-in
    # blockers raised today.
    blocked_tasks = (
        Task.query.filter(Task.status == TaskStatus.BLOCKED.value)
        .order_by(Task.updated_at.asc())
        .limit(limit)
        .all()
    )

    active_members = User.query.filter_by(is_active_flag=True).all()
    missing_checkins = [
        user
        for user in active_members
        if is_working_day_for(user, today)
        and DailyCheckin.query.filter_by(user_id=user.id, workday_date=today).first() is None
    ]

    overdue_milestones = (
        Milestone.query.filter(
            Milestone.status.notin_(
                [MilestoneStatus.COMPLETED.value, MilestoneStatus.CANCELLED.value]
            ),
            Milestone.due_date.isnot(None),
            Milestone.due_date < today,
        )
        .order_by(Milestone.due_date.asc())
        .limit(limit)
        .all()
    )

    return {
        "overdue_tasks": overdue,
        "awaiting_review": awaiting_review,
        "open_blockers": open_blockers,
        "blocked_tasks": blocked_tasks,
        "missing_checkins": missing_checkins,
        "overdue_milestones": overdue_milestones,
        "total": (
            len(overdue)
            + len(awaiting_review)
            + len(open_blockers)
            + len(missing_checkins)
            + len(overdue_milestones)
        ),
    }


def recent_activity(*, limit: int = 15, member: User | None = None) -> list[AuditLog]:
    query = AuditLog.query
    if member is not None:
        query = query.filter(AuditLog.user_id == member.id)
    return query.order_by(AuditLog.created_at.desc()).limit(limit).all()


def member_workload(user) -> dict:
    """A member's current load, for the CEO's team view."""
    open_tasks = Task.query.filter(
        Task.assignee_id == user.id,
        Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
    ).count()
    overdue = Task.query.filter(
        Task.assignee_id == user.id,
        Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
        Task.due_date.isnot(None),
        Task.due_date < company_today(),
    ).count()
    awaiting_review = Task.query.filter(
        Task.reviewer_id == user.id, Task.status == TaskStatus.IN_REVIEW.value
    ).count()
    projects = (
        Project.query.join(ProjectMember, ProjectMember.project_id == Project.id)
        .filter(ProjectMember.user_id == user.id)
        .count()
    )
    return {
        "open_tasks": open_tasks,
        "overdue": overdue,
        "awaiting_my_review": awaiting_review,
        "projects": projects,
    }


def weekly_digest(*, start: date | None = None) -> dict:
    """One week of figures for the reports page."""
    today = company_today()
    week_start, week_end = week_bounds(start or today)
    return {
        "start": week_start,
        "end": week_end,
        "tasks": task_metrics(start=week_start, end=week_end),
        "reviews": review_metrics(start=week_start, end=week_end),
        "focus": focus_metrics(start=week_start, end=week_end),
        "reporting": reporting_metrics(start=week_start, end=week_end),
        "members": compliance_by_member(start=week_start, end=week_end),
    }
