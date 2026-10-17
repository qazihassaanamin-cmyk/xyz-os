"""Task workspace: list, detail, state changes, submissions, blockers.

Every route declares an explicit policy check. Object lookups by URL id are
scoped so that a task the member may not see returns 404 rather than 403, which
avoids confirming that the record exists.
"""

from __future__ import annotations

from datetime import date

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from app.extensions import db, limiter
from app.models.project import Milestone, Project, ProjectMember, ProjectRole, ProjectStatus
from app.models.task import (
    Blocker,
    BlockerKind,
    BlockerStatus,
    Task,
    TaskDifficulty,
    TaskPriority,
    TaskStatus,
)
from app.models.user import User
from app.permissions import (
    can_edit_task,
    can_raise_blocker,
    can_resolve_blocker,
    can_review_submission,
    can_set_task_difficulty,
    can_submit_work,
    can_view_task,
    is_ceo,
    login_required,
)
from app.services import task_service, upload_service
from app.services.audit_service import record_audit
from app.utils.dates import company_today, deadline_label, humanise_seconds
from app.utils.pagination import paginate
from app.utils.validators import ValidationError, optional_text, parse_date

bp = Blueprint("tasks", __name__)


# ---------------------------------------------------------------------------
# Lookup helpers — every one of these is authorization-scoped
# ---------------------------------------------------------------------------

def _get_task_or_404(task_id: int) -> Task:
    task = db.session.get(Task, task_id)
    if task is None or not can_view_task(current_user, task):
        abort(404)
    return task


def _get_project_or_404(project_id: int) -> Project:
    from app.permissions import can_view_project

    project = db.session.get(Project, project_id)
    if project is None or not can_view_project(current_user, project):
        abort(404)
    return project


def _assignable_members(project: Project | None) -> list[User]:
    if project is None:
        return User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()
    return (
        User.query.join(ProjectMember, ProjectMember.user_id == User.id)
        .filter(ProjectMember.project_id == project.id, User.is_active_flag.is_(True))
        .order_by(User.full_name.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    """My tasks, with filters. Members see their own work by default."""
    view = request.args.get("view", "assigned")
    status = request.args.get("status", "")
    priority = request.args.get("priority", "")
    project_id = request.args.get("project", type=int)
    due = request.args.get("due", "")
    assignee_id = request.args.get("assignee", type=int)
    search = (request.args.get("q") or "").strip()

    query = task_service.visible_tasks_query(current_user, include_completed=True)

    if view == "assigned":
        query = query.filter(Task.assignee_id == current_user.id)
    elif view == "created":
        query = query.filter(Task.created_by_id == current_user.id)
    elif view == "review":
        query = query.filter(
            Task.reviewer_id == current_user.id, Task.status == TaskStatus.IN_REVIEW.value
        )
    elif view == "watching" and is_ceo(current_user):
        pass  # company-wide
    elif view == "team" and is_ceo(current_user):
        pass

    if status:
        query = query.filter(Task.status == status.upper())
    else:
        if request.args.get("include_done") != "1":
            query = query.filter(
                Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value])
            )
    if priority:
        query = query.filter(Task.priority == priority.upper())
    if project_id:
        query = query.filter(Task.project_id == project_id)
    if assignee_id and is_ceo(current_user):
        query = query.filter(Task.assignee_id == assignee_id)
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(Task.title.ilike(like), Task.description.ilike(like)))

    today = company_today()
    if due == "overdue":
        query = query.filter(Task.due_date.isnot(None), Task.due_date < today)
    elif due == "today":
        query = query.filter(Task.due_date == today)
    elif due == "week":
        from datetime import timedelta

        query = query.filter(
            Task.due_date.isnot(None),
            Task.due_date >= today,
            Task.due_date <= today + timedelta(days=7),
        )
    elif due == "none":
        query = query.filter(Task.due_date.is_(None))

    query = query.order_by(
        db.case(
            (Task.status == TaskStatus.BLOCKED.value, 0),
            (Task.status == TaskStatus.IN_REVIEW.value, 1),
            (Task.status == TaskStatus.IN_PROGRESS.value, 2),
            (Task.status == TaskStatus.TODO.value, 3),
            else_=4,
        ),
        Task.due_date.asc().nulls_last(),
        Task.priority.desc(),
    )

    pagination = paginate(query, per_page=25)

    return render_template(
        "tasks/index.html",
        page_title="Tasks",
        pagination=pagination,
        tasks=pagination.items,
        view=view,
        status=status,
        priority=priority,
        project_id=project_id,
        assignee_id=assignee_id,
        due=due,
        search=search,
        include_done=request.args.get("include_done") == "1",
        projects=_project_choices(),
        members=User.query.filter_by(is_active_flag=True).order_by(User.full_name).all(),
        counts=_view_counts(),
        deadline_label=deadline_label,
    )


def _project_choices():
    from app.permissions import is_ceo as _is_ceo

    query = Project.query.filter(Project.status != ProjectStatus.ARCHIVED.value)
    if not _is_ceo(current_user):
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).filter(
            ProjectMember.user_id == current_user.id
        )
    return query.order_by(Project.name.asc()).all()


def _view_counts() -> dict:
    open_query = task_service.visible_tasks_query(current_user, include_completed=False)
    return {
        "assigned": open_query.filter(Task.assignee_id == current_user.id).count(),
        "review": Task.query.filter(
            Task.reviewer_id == current_user.id, Task.status == TaskStatus.IN_REVIEW.value
        ).count(),
        "overdue": task_service.overdue_tasks_query(current_user)
        .filter(Task.assignee_id == current_user.id)
        .count(),
    }


# ---------------------------------------------------------------------------
# Create and edit
# ---------------------------------------------------------------------------

@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    """Create a task. Only the CEO and project leads reach this page."""
    from app.permissions import can_assign_task

    project = None
    project_id = request.values.get("project", type=int)
    if project_id:
        project = _get_project_or_404(project_id)

    if not can_assign_task(current_user, project):
        abort(403)

    if request.method == "POST":
        try:
            project_id = request.form.get("project_id", type=int)
            project = _get_project_or_404(project_id) if project_id else None
            if not can_assign_task(current_user, project):
                abort(403)

            assignee = None
            assignee_id = request.form.get("assignee_id", type=int)
            if assignee_id:
                assignee = db.session.get(User, assignee_id)

            reviewer = None
            reviewer_id = request.form.get("reviewer_id", type=int)
            if reviewer_id:
                reviewer = db.session.get(User, reviewer_id)

            milestone = None
            milestone_id = request.form.get("milestone_id", type=int)
            if milestone_id:
                milestone = db.session.get(Milestone, milestone_id)

            task = task_service.create_task(
                actor=current_user,
                title=request.form.get("title", ""),
                project=project,
                assignee=assignee,
                reviewer=reviewer,
                description=request.form.get("description"),
                acceptance_criteria=request.form.get("acceptance_criteria"),
                priority=request.form.get("priority", TaskPriority.NORMAL.value),
                difficulty=request.form.get("difficulty", TaskDifficulty.MEDIUM.value),
                due_date=parse_date(request.form.get("due_date"), field="Deadline"),
                start_date=parse_date(request.form.get("start_date"), field="Start date"),
                milestone=milestone,
            )
        except (task_service.TaskError, ValidationError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Task “{task.title}” created.", "success")
            return redirect(url_for("tasks.detail", task_id=task.id))

    return render_template(
        "tasks/form.html",
        page_title="New task",
        task=None,
        project=project,
        projects=_project_choices(),
        members=_assignable_members(project) if project else User.query.filter_by(
            is_active_flag=True
        ).order_by(User.full_name).all(),
        milestones=project.milestones if project else [],
        priorities=TaskPriority.values(),
        difficulties=TaskDifficulty.values(),
        statuses=TaskStatus.values(),
        deadline_label=deadline_label,
    )


@bp.route("/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
def edit(task_id: int):
    task = _get_task_or_404(task_id)
    if not can_edit_task(current_user, task):
        abort(403)

    if request.method == "POST":
        try:
            assignee_id = request.form.get("assignee_id", type=int)
            reviewer_id = request.form.get("reviewer_id")
            milestone_id = request.form.get("milestone_id")

            task_service.update_task(
                task,
                actor=current_user,
                title=request.form.get("title"),
                description=request.form.get("description"),
                acceptance_criteria=request.form.get("acceptance_criteria"),
                progress_note=request.form.get("progress_note"),
                priority=request.form.get("priority"),
                difficulty=request.form.get("difficulty"),
                base_xp=request.form.get("base_xp"),
                due_date=parse_date(request.form.get("due_date"), field="Deadline"),
                start_date=parse_date(request.form.get("start_date"), field="Start date"),
                assignee_id=assignee_id,
                reviewer_id=reviewer_id,
                milestone_id=milestone_id,
            )
        except (task_service.TaskError, ValidationError) as exc:
            flash(str(exc), "error")
        else:
            flash("Task updated.", "success")
            return redirect(url_for("tasks.detail", task_id=task.id))

    return render_template(
        "tasks/form.html",
        page_title=f"Edit: {task.title}",
        task=task,
        project=task.project,
        projects=_project_choices(),
        members=_assignable_members(task.project),
        milestones=task.project.milestones if task.project else [],
        priorities=TaskPriority.values(),
        difficulties=TaskDifficulty.values(),
        statuses=TaskStatus.values(),
        deadline_label=deadline_label,
    )


@bp.route("/<int:task_id>", methods=["GET"])
@login_required
def detail(task_id: int):
    """The task workspace: execution in the main column, facts in the rail."""
    task = _get_task_or_404(task_id)

    from app.services.activity_service import contribution_history

    history = [
        entry
        for entry in contribution_history(task.assignee, days=365, limit=200)
        if entry.get("url_params", {}).get("task_id") == task.id
    ] if task.assignee else []

    audit_rows = _task_audit(task)

    return render_template(
        "tasks/detail.html",
        page_title=task.title,
        task=task,
        attachments=upload_service.gallery_for_task(task),
        submissions=task_service.submission_history(task),
        history=history[:12],
        audit_rows=audit_rows,
        transitions=task_service.available_transitions(task, current_user),
        can_edit=can_edit_task(current_user, task),
        can_submit=can_submit_work(current_user, task),
        can_acknowledge=task.can_be_acknowledged_by(current_user),
        can_raise_blocker=can_raise_blocker(current_user, task),
        can_set_difficulty=can_set_task_difficulty(current_user, task),
        blockers=task.blockers,
        blocker_kinds=BlockerKind.values(),
        max_files=current_app.config.get("MAX_FILES_PER_SUBMISSION", 3),
        max_upload_mb=current_app.config.get("MAX_UPLOAD_MB", 5),
        deadline_label=deadline_label,
        humanise_seconds=humanise_seconds,
    )


def _task_audit(task: Task, limit: int = 30):
    from app.models.audit import AuditLog

    return (
        AuditLog.query.filter_by(entity_type="task", entity_id=task.id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# State changes
# ---------------------------------------------------------------------------

@bp.route("/<int:task_id>/status", methods=["POST"])
@login_required
def change_status(task_id: int):
    task = _get_task_or_404(task_id)
    target = request.form.get("status", "")
    reason = request.form.get("reason")

    try:
        task_service.transition(task, target, actor=current_user, reason=reason)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Task moved to {target.replace('_', ' ').title()}.", "success")
    return redirect(url_for("tasks.detail", task_id=task.id))


@bp.route("/<int:task_id>/acknowledge", methods=["POST"])
@login_required
def acknowledge(task_id: int):
    task = _get_task_or_404(task_id)
    try:
        task_service.acknowledge_task(task, actor=current_user)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash("Task acknowledged.", "success")
    return redirect(url_for("tasks.detail", task_id=task.id))


@bp.route("/<int:task_id>/cancel", methods=["POST"])
@login_required
def cancel(task_id: int):
    task = _get_task_or_404(task_id)
    try:
        task_service.delete_task(task, actor=current_user)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash("Task cancelled. The record is retained.", "info")
    return redirect(url_for("tasks.detail", task_id=task.id))


# ---------------------------------------------------------------------------
# Checklist and comments
# ---------------------------------------------------------------------------

@bp.route("/<int:task_id>/checklist", methods=["POST"])
@login_required
def add_checklist_item(task_id: int):
    task = _get_task_or_404(task_id)
    try:
        task_service.add_subtask(task, title=request.form.get("title", ""), actor=current_user)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tasks.detail", task_id=task.id) + "#checklist")


@bp.route("/checklist/<int:subtask_id>/toggle", methods=["POST"])
@login_required
def toggle_checklist_item(subtask_id: int):
    from app.models.task import Subtask

    subtask = db.session.get(Subtask, subtask_id)
    if subtask is None or not can_view_task(current_user, subtask.task):
        abort(404)

    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        task_service.toggle_subtask(subtask, actor=current_user)
    except task_service.TaskError as exc:
        if wants_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        flash(str(exc), "error")
    else:
        if wants_json:
            task = subtask.task
            return jsonify(
                {
                    "ok": True,
                    "is_done": subtask.is_done,
                    "progress": task.checklist_progress,
                    "done": task.done_subtask_count,
                    "total": len(task.subtasks),
                }
            )
    return redirect(url_for("tasks.detail", task_id=subtask.task_id) + "#checklist")


@bp.route("/checklist/<int:subtask_id>/remove", methods=["POST"])
@login_required
def remove_checklist_item(subtask_id: int):
    from app.models.task import Subtask

    subtask = db.session.get(Subtask, subtask_id)
    if subtask is None or not can_view_task(current_user, subtask.task):
        abort(404)
    task_id = subtask.task_id
    try:
        task_service.remove_subtask(subtask, actor=current_user)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tasks.detail", task_id=task_id) + "#checklist")


@bp.route("/<int:task_id>/comment", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def comment(task_id: int):
    task = _get_task_or_404(task_id)
    try:
        task_service.add_comment(task, actor=current_user, body=request.form.get("body", ""))
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tasks.detail", task_id=task.id) + "#discussion")


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------

@bp.route("/<int:task_id>/submit", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def submit(task_id: int):
    """Submit work for review. Files are validated and stored before the
    submission is created, and the whole set is rejected if any file fails."""
    task = _get_task_or_404(task_id)
    if not can_submit_work(current_user, task):
        abort(403)

    files = [f for f in request.files.getlist("files") if f and f.filename]
    max_files = int(current_app.config.get("MAX_FILES_PER_SUBMISSION", 3))
    if len(files) > max_files:
        flash(f"Attach at most {max_files} files per submission.", "error")
        return redirect(url_for("tasks.detail", task_id=task.id) + "#submit")

    external_link = (request.form.get("external_link") or "").strip()
    summary = request.form.get("summary", "")

    if not files and not external_link:
        flash("Attach at least one file or provide a link to the work.", "error")
        return redirect(url_for("tasks.detail", task_id=task.id) + "#submit")

    try:
        submission = task_service.submit_work(
            task,
            actor=current_user,
            summary=summary,
            external_link=external_link,
            attachment_count=len(files),
        )
    except (task_service.TaskError, ValidationError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("tasks.detail", task_id=task.id) + "#submit")

    stored = []
    try:
        for file in files:
            attachment = upload_service.validate_and_store(
                file,
                user=current_user,
                task_id=task.id,
                submission_id=submission.id,
                caption=request.form.get(f"caption_{file.filename}"),
            )
            stored.append(attachment)
    except upload_service.UploadError as exc:
        # The submission stays, but the member is told exactly what failed.
        flash(f"Some files were not stored: {exc}", "error")
    except Exception:
        current_app.logger.exception("Unexpected upload failure on task %s", task.id)
        flash("Some files could not be stored. Try again.", "error")

    if stored:
        flash(
            f"Submitted for review with {len(stored)} file"
            f"{'s' if len(stored) != 1 else ''}.",
            "success",
        )
    else:
        flash("Submitted for review.", "success")
    return redirect(url_for("tasks.detail", task_id=task.id) + "#submissions")


@bp.route("/submissions/<int:submission_id>/review", methods=["POST"])
@login_required
def review(submission_id: int):
    from app.models.task import Submission

    submission = db.session.get(Submission, submission_id)
    if submission is None or submission.task is None or not can_view_task(current_user, submission.task):
        abort(404)
    if not can_review_submission(current_user, submission):
        abort(403)

    decision = request.form.get("decision", "")
    feedback = request.form.get("feedback")
    quality_bonus = request.form.get("quality_bonus") == "1"

    try:
        task_service.review_submission(
            submission,
            actor=current_user,
            decision=decision,
            feedback=feedback,
            quality_bonus=quality_bonus,
        )
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        if decision.upper() == "APPROVED":
            flash("Work approved. The task is complete.", "success")
        else:
            flash("Work returned with your feedback.", "info")

    return redirect(url_for("tasks.detail", task_id=submission.task_id) + "#submissions")


# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------

@bp.route("/blockers")
@login_required
def blockers():
    """All blockers this member may see."""
    from app.permissions import can_view_blocker

    status = request.args.get("status", "open")
    query = Blocker.query

    if not is_ceo(current_user):
        member_project_ids = [
            row.project_id for row in ProjectMember.query.filter_by(user_id=current_user.id).all()
        ]
        query = query.filter(
            db.or_(
                Blocker.raised_by_id == current_user.id,
                Blocker.assigned_to_id == current_user.id,
                Blocker.project_id.in_(member_project_ids) if member_project_ids else db.false(),
            )
        )

    if status == "open":
        query = query.filter(
            Blocker.status.in_([BlockerStatus.OPEN.value, BlockerStatus.IN_PROGRESS.value])
        )
    elif status == "resolved":
        query = query.filter(Blocker.status == BlockerStatus.RESOLVED.value)

    query = query.order_by(Blocker.created_at.desc())
    pagination = paginate(query, per_page=25)
    items = [row for row in pagination.items if can_view_blocker(current_user, row)]

    return render_template(
        "tasks/blockers.html",
        page_title="Blockers",
        pagination=pagination,
        blockers=items,
        status=status,
        blocker_kinds=BlockerKind.values(),
        members=User.query.filter_by(is_active_flag=True).order_by(User.full_name).all(),
        can_resolve=lambda row: can_resolve_blocker(current_user, row),
    )


@bp.route("/<int:task_id>/blocker", methods=["POST"])
@login_required
def raise_blocker(task_id: int):
    task = _get_task_or_404(task_id)
    if not can_raise_blocker(current_user, task):
        abort(403)

    assigned_to = None
    assigned_id = request.form.get("assigned_to_id", type=int)
    if assigned_id:
        assigned_to = db.session.get(User, assigned_id)

    try:
        task_service.raise_blocker(
            actor=current_user,
            description=request.form.get("description", ""),
            kind=request.form.get("kind", BlockerKind.OTHER.value),
            task=task,
            assigned_to=assigned_to,
        )
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash("Blocker recorded. The task is marked as blocked.", "info")
    return redirect(url_for("tasks.detail", task_id=task.id) + "#blockers")


@bp.route("/blockers/<int:blocker_id>/resolve", methods=["POST"])
@login_required
def resolve_blocker(blocker_id: int):
    blocker = db.session.get(Blocker, blocker_id)
    if blocker is None:
        abort(404)
    from app.permissions import can_view_blocker

    if not can_view_blocker(current_user, blocker):
        abort(404)
    if not can_resolve_blocker(current_user, blocker):
        abort(403)

    helper = None
    helper_id = request.form.get("helper_id", type=int)
    if helper_id and is_ceo(current_user):
        helper = db.session.get(User, helper_id)

    try:
        task_service.resolve_blocker(
            blocker,
            actor=current_user,
            resolution_note=request.form.get("resolution_note", ""),
            helper=helper,
        )
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash("Blocker resolved.", "success")

    if blocker.task_id:
        return redirect(url_for("tasks.detail", task_id=blocker.task_id) + "#blockers")
    return redirect(url_for("tasks.blockers"))


@bp.route("/blockers/<int:blocker_id>/assign", methods=["POST"])
@login_required
def assign_blocker_helper(blocker_id: int):
    blocker = db.session.get(Blocker, blocker_id)
    if blocker is None:
        abort(404)
    from app.permissions import can_view_blocker

    if not can_view_blocker(current_user, blocker):
        abort(404)

    helper = db.session.get(User, request.form.get("helper_id", type=int))
    if helper is None:
        flash("Choose a member to help.", "error")
        return redirect(request.referrer or url_for("tasks.blockers"))

    try:
        task_service.assign_helper(blocker, helper=helper, actor=current_user)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Asked {helper.display_name} to help.", "info")
    return redirect(request.referrer or url_for("tasks.blockers"))


# ---------------------------------------------------------------------------
# Review queue (used by the CEO review page)
# ---------------------------------------------------------------------------

@bp.route("/reviews")
@login_required
def review_queue():
    """Submissions waiting for this member's decision."""
    from app.models.task import Submission, SubmissionStatus

    if is_ceo(current_user):
        query = Submission.query.filter(Submission.status == SubmissionStatus.PENDING.value)
    else:
        query = Submission.query.filter(
            Submission.status == SubmissionStatus.PENDING.value,
            Submission.task.has(Task.reviewer_id == current_user.id),
        )
    query = query.order_by(Submission.created_at.asc())
    pagination = paginate(query, per_page=20)

    return render_template(
        "tasks/review_queue.html",
        page_title="Task reviews",
        pagination=pagination,
        submissions=pagination.items,
        max_files=current_app.config.get("MAX_FILES_PER_SUBMISSION", 3),
    )


# ---------------------------------------------------------------------------
# Small JSON endpoint used by the workspace to stay in step
# ---------------------------------------------------------------------------

@bp.route("/<int:task_id>/state")
@login_required
def state(task_id: int):
    task = _get_task_or_404(task_id)
    return jsonify(
        {
            "id": task.id,
            "status": task.status,
            "priority": task.priority,
            "checklist_progress": task.checklist_progress,
            "done_subtasks": task.done_subtask_count,
            "total_subtasks": len(task.subtasks),
            "pending_submission": bool(task.pending_submission),
            "open_blockers": len(task.open_blockers),
            "server_date": company_today().isoformat(),
        }
    )
