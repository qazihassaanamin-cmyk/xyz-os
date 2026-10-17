"""Personal and CEO dashboards.

Two distinct pages, because they answer different questions:

* The member dashboard answers "what should I do now, and where do I stand?"
* The CEO command centre answers "what needs my attention, and what is the
  state of the company?" — dense, table-led, with every figure linking to the
  records behind it.

Neither page invents data. Where there is nothing to show, the page says what
to do next.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, render_template, request
from flask_login import current_user

from app.extensions import db
from app.models.daily import DailyCheckin, DailyReport
from app.models.focus import FocusSession, FocusStatus
from app.models.notification import Notification
from app.models.project import Milestone, MilestoneStatus, Project, ProjectMember, ProjectStatus
from app.models.task import Blocker, BlockerStatus, Submission, SubmissionStatus, Task, TaskStatus
from app.models.user import User
from app.permissions import login_required
from app.services import daily_service, focus_service, report_service, task_service
from app.utils.dates import company_today, deadline_label
from app.utils.pagination import paginate
from app.utils.validators import nulls_last_asc

bp = Blueprint("dashboard", __name__)


def _member_task_buckets(user) -> dict:
    """The member's own work, bucketed by what it needs from them."""
    today = company_today()
    open_query = task_service.visible_tasks_query(user, include_completed=False)

    today_tasks = (
        open_query.filter(Task.due_date == today, Task.assignee_id == user.id)
        .order_by(Task.priority.desc())
        .limit(10)
        .all()
    )
    high_priority = (
        open_query.filter(
            Task.assignee_id == user.id,
            Task.priority.in_(["HIGH", "CRITICAL"]),
        )
        .order_by(*nulls_last_asc(Task.due_date), Task.priority.desc())
        .limit(10)
        .all()
    )
    overdue = (
        task_service.overdue_tasks_query(user)
        .filter(Task.assignee_id == user.id)
        .limit(10)
        .all()
    )
    upcoming = (
        open_query.filter(
            Task.assignee_id == user.id,
            Task.due_date.isnot(None),
            Task.due_date > today,
        )
        .order_by(Task.due_date.asc())
        .limit(8)
        .all()
    )
    awaiting_my_review = (
        Task.query.filter(Task.reviewer_id == user.id, Task.status == TaskStatus.IN_REVIEW.value)
        .order_by(Task.updated_at.asc())
        .limit(10)
        .all()
    )
    my_submissions_pending = (
        Submission.query.filter(
            Submission.submitted_by_id == user.id,
            Submission.status == SubmissionStatus.PENDING.value,
        )
        .order_by(Submission.created_at.asc())
        .limit(10)
        .all()
    )
    needs_acknowledgement = (
        open_query.filter(
            Task.assignee_id == user.id,
            Task.acknowledged_at.is_(None),
            Task.status.in_([TaskStatus.TODO.value, TaskStatus.IN_PROGRESS.value]),
        )
        .order_by(Task.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "today": today_tasks,
        "high_priority": high_priority,
        "overdue": overdue,
        "upcoming": upcoming,
        "awaiting_review": awaiting_my_review,
        "pending_submissions": my_submissions_pending,
        "needs_acknowledgement": needs_acknowledgement,
    }


def _xp_panel(user) -> dict:
    from app.services import achievement_service, challenge_service, xp_service
    from app.services.activity_service import weekly_progress

    profile = xp_service.ensure_profile(user)
    recent_awards = xp_service.history_for(user, limit=5)
    return {
        "profile": profile,
        "recent_awards": recent_awards,
        "achievements": achievement_service.for_user(user)[:4],
        "badges": _active_badges(user),
        "challenges": challenge_service.summary_for(user),
        "weekly": weekly_progress(user),
    }


def _active_badges(user):
    from app.services.badge_service import active_for

    return active_for(user)[:4]


@bp.route("/dashboard")
@login_required
def index():
    """The member's own dashboard."""
    buckets = _member_task_buckets(current_user)
    daily = daily_service.today_status(current_user)
    focus_stats = focus_service.stats_for(current_user, days=30)

    my_projects = (
        ProjectMember.query.filter_by(user_id=current_user.id)
        .join(ProjectMember.project)
        .filter(Project.status != ProjectStatus.ARCHIVED.value)
        .all()
    )

    milestones = (
        Milestone.query.join(ProjectMember, ProjectMember.project_id == Milestone.project_id)
        .filter(
            ProjectMember.user_id == current_user.id,
            Milestone.status.notin_(
                [MilestoneStatus.COMPLETED.value, MilestoneStatus.CANCELLED.value]
            ),
        )
        .order_by(*nulls_last_asc(Milestone.due_date))
        .limit(6)
        .all()
    )

    open_blockers = (
        Blocker.query.filter(
            Blocker.raised_by_id == current_user.id,
            Blocker.status.in_([BlockerStatus.OPEN.value, BlockerStatus.IN_PROGRESS.value]),
        )
        .order_by(Blocker.created_at.desc())
        .limit(5)
        .all()
    )

    recent_feedback = (
        Submission.query.filter(
            Submission.submitted_by_id == current_user.id,
            Submission.status == SubmissionStatus.CHANGES_REQUESTED.value,
        )
        .order_by(Submission.updated_at.desc())
        .limit(5)
        .all()
    )

    from app.services.activity_service import contribution_history

    return render_template(
        "dashboard/index.html",
        page_title="My dashboard",
        buckets=buckets,
        daily=daily,
        focus_stats=focus_stats,
        projects=my_projects,
        milestones=milestones,
        open_blockers=open_blockers,
        recent_feedback=recent_feedback,
        xp_panel=_xp_panel(current_user),
        activity=contribution_history(current_user, days=14, limit=8),
        deadline_label=deadline_label,
    )


@bp.route("/dashboard/ceo")
@login_required
def ceo():
    """CEO command centre: attention first, then company figures."""
    if not current_user.is_ceo:
        abort(403)

    from app.services.xp_service import leaderboard
    from app.services.activity_service import weekly_progress

    # Date range and filters are server-rendered query parameters.
    days = request.args.get("days", "30")
    try:
        days_int = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days_int = 30

    project_id = request.args.get("project", type=int)
    member_id = request.args.get("member", type=int)

    project = None
    if project_id:
        from app.models.project import Project

        project = db.session.get(Project, project_id)

    member = None
    if member_id:
        member = db.session.get(User, member_id)

    start, end = report_service.parse_range(None, None, default_days=days_int)

    queue = report_service.attention_queue()
    members = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()

    return render_template(
        "dashboard/ceo.html",
        page_title="Command centre",
        queue=queue,
        members=members,
        projects=_project_options(),
        selected_project=project,
        selected_member=member,
        days=days_int,
        start=start,
        end=end,
        task_metrics=report_service.task_metrics(
            start=start, end=end, project=project, member=member
        ),
        review_metrics=report_service.review_metrics(
            start=start, end=end, project=project, member=member
        ),
        focus_metrics=report_service.focus_metrics(start=start, end=end, member=member),
        reporting_metrics=report_service.reporting_metrics(start=start, end=end, member=member),
        compliance=report_service.compliance_by_member(start=start, end=end, members=members),
        activity=report_service.recent_activity(limit=15),
        leaderboard=leaderboard(limit=8),
        unread_company=Notification.query.filter(Notification.read_at.is_(None)).count(),
        storage=_storage_snapshot(),
        deadline_label=deadline_label,
        weekly_progress=weekly_progress,
    )


def _project_options():
    from app.models.project import Project, ProjectStatus

    return (
        Project.query.filter(Project.status != ProjectStatus.ARCHIVED.value)
        .order_by(Project.name.asc())
        .all()
    )


def _storage_snapshot() -> dict | None:
    """Disk usage, shown only when it is genuinely useful."""
    try:
        from app.services.upload_service import storage_usage

        return storage_usage()
    except Exception:  # pragma: no cover
        return None


@bp.route("/dashboard/attention")
@login_required
def attention():
    """The full attention queue, paginated, with the same links as the summary."""
    if not current_user.is_ceo:
        abort(403)

    kind = request.args.get("kind", "overdue")
    page = request.args.get("page")

    if kind == "review":
        query = (
            Submission.query.filter(Submission.status == SubmissionStatus.PENDING.value)
            .order_by(Submission.created_at.asc())
        )
        title = "Work awaiting review"
    elif kind == "blockers":
        query = Blocker.query.filter(
            Blocker.status.in_([BlockerStatus.OPEN.value, BlockerStatus.IN_PROGRESS.value])
        ).order_by(Blocker.created_at.asc())
        title = "Open blockers"
    elif kind == "milestones":
        query = Milestone.query.filter(
            Milestone.status.notin_(
                [MilestoneStatus.COMPLETED.value, MilestoneStatus.CANCELLED.value]
            ),
            Milestone.due_date.isnot(None),
            Milestone.due_date < company_today(),
        ).order_by(Milestone.due_date.asc())
        title = "Overdue milestones"
    elif kind == "checkins":
        today = company_today()
        submitted = {
            row.user_id for row in DailyCheckin.query.filter_by(workday_date=today).all()
        }
        users = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()
        missing = [user for user in users if user.id not in submitted]
        return render_template(
            "dashboard/attention.html",
            page_title="Members with no check-in today",
            kind=kind,
            title="Members with no check-in today",
            users=missing,
            pagination=None,
        )
    else:
        query = task_service.overdue_tasks_query().order_by(Task.due_date.asc())
        title = "Overdue tasks"

    pagination = paginate(query, page=page, per_page=25)
    return render_template(
        "dashboard/attention.html",
        page_title=title,
        kind=kind,
        title=title,
        pagination=pagination,
        items=pagination.items,
    )
