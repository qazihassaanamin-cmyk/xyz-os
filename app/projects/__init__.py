"""Projects: list, detail, membership, milestones."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models.project import (
    Milestone,
    MilestoneStatus,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectStatus,
)
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.permissions import (
    can_create_project,
    can_manage_project,
    can_view_project,
    is_ceo,
    login_required,
)
from app.services import project_service, report_service, task_service, upload_service
from app.utils.dates import company_today, deadline_label
from app.utils.pagination import paginate
from app.utils.validators import ValidationError, nulls_last_asc, parse_date

bp = Blueprint("projects", __name__)


def _get_project_or_404(project_id: int) -> Project:
    project = db.session.get(Project, project_id)
    if project is None or not can_view_project(current_user, project):
        abort(404)
    return project


@bp.route("/")
@login_required
def index():
    """Projects this member is part of."""
    include_archived = request.args.get("archived") == "1"
    rows = project_service.summary_for_user(current_user)

    if not include_archived:
        rows = [row for row in rows if row["project"].status != ProjectStatus.ARCHIVED.value]

    return render_template(
        "projects/index.html",
        page_title="Projects",
        rows=rows,
        include_archived=include_archived,
        can_create=can_create_project(current_user),
        deadline_label=deadline_label,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if not can_create_project(current_user):
        abort(403)

    if request.method == "POST":
        members = []
        for member_id in request.form.getlist("member_ids", type=int):
            member = db.session.get(User, member_id)
            if member is not None:
                members.append(member)

        owner = None
        owner_id = request.form.get("owner_id", type=int)
        if owner_id:
            owner = db.session.get(User, owner_id)

        try:
            project = project_service.create_project(
                actor=current_user,
                name=request.form.get("name", ""),
                description=request.form.get("description"),
                objectives=request.form.get("objectives"),
                status=request.form.get("status", ProjectStatus.PLANNING.value),
                owner=owner,
                start_date=parse_date(request.form.get("start_date"), field="Start date"),
                target_date=parse_date(request.form.get("target_date"), field="Target date"),
                members=members,
            )
        except (project_service.ProjectError, ValidationError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Project “{project.name}” created.", "success")
            return redirect(url_for("projects.detail", project_id=project.id))

    return render_template(
        "projects/form.html",
        page_title="New project",
        project=None,
        members=User.query.filter_by(is_active_flag=True).order_by(User.full_name).all(),
        statuses=ProjectStatus.values(),
    )


@bp.route("/<int:project_id>")
@login_required
def detail(project_id: int):
    project = _get_project_or_404(project_id)
    progress = project_service.progress_for(project)

    status_filter = request.args.get("status", "")
    task_query = Task.query.filter(Task.project_id == project.id)
    if status_filter:
        task_query = task_query.filter(Task.status == status_filter.upper())
    else:
        task_query = task_query.filter(
            Task.status.notin_([TaskStatus.CANCELLED.value])
        )
    task_query = task_query.order_by(Task.status.asc(), *nulls_last_asc(Task.due_date))
    pagination = paginate(task_query, per_page=25)

    return render_template(
        "projects/detail.html",
        page_title=project.name,
        project=project,
        progress=progress,
        pagination=pagination,
        tasks=pagination.items,
        status_filter=status_filter,
        memberships=project_service.membership_rows(project),
        can_manage=can_manage_project(current_user, project),
        is_ceo=is_ceo(current_user),
        summary=report_service.project_summary(project=project),
        all_members=User.query.filter_by(is_active_flag=True).order_by(User.full_name).all(),
        statuses=ProjectStatus.values(),
        task_statuses=TaskStatus.values(),
        milestone_statuses=MilestoneStatus.values(),
        deadline_label=deadline_label,
        attachments=upload_service.gallery_for_task,
    )


@bp.route("/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def edit(project_id: int):
    project = _get_project_or_404(project_id)
    if not can_manage_project(current_user, project):
        abort(403)

    if request.method == "POST":
        try:
            project_service.update_project(
                project,
                actor=current_user,
                name=request.form.get("name"),
                description=request.form.get("description"),
                objectives=request.form.get("objectives"),
                status=request.form.get("status"),
                start_date=parse_date(request.form.get("start_date"), field="Start date"),
                target_date=parse_date(request.form.get("target_date"), field="Target date"),
                owner_id=request.form.get("owner_id", type=int),
            )
        except (project_service.ProjectError, ValidationError) as exc:
            flash(str(exc), "error")
        else:
            flash("Project updated.", "success")
            return redirect(url_for("projects.detail", project_id=project.id))

    return render_template(
        "projects/form.html",
        page_title=f"Edit: {project.name}",
        project=project,
        members=User.query.filter_by(is_active_flag=True).order_by(User.full_name).all(),
        statuses=ProjectStatus.values(),
    )


@bp.route("/<int:project_id>/members", methods=["POST"])
@login_required
def add_member(project_id: int):
    project = _get_project_or_404(project_id)
    member = db.session.get(User, request.form.get("user_id", type=int))
    if member is None:
        flash("Choose a member.", "error")
        return redirect(url_for("projects.detail", project_id=project.id))

    try:
        project_service.add_member(
            project,
            member=member,
            actor=current_user,
            role_in_project=request.form.get("role_in_project", ProjectRole.MEMBER.value),
        )
    except project_service.ProjectError as exc:
        flash(str(exc), "error")
    else:
        flash(f"{member.display_name} added to the project.", "success")
    return redirect(url_for("projects.detail", project_id=project.id) + "#team")


@bp.route("/<int:project_id>/members/<int:user_id>/remove", methods=["POST"])
@login_required
def remove_member(project_id: int, user_id: int):
    project = _get_project_or_404(project_id)
    member = db.session.get(User, user_id)
    if member is None:
        abort(404)

    try:
        project_service.remove_member(project, member=member, actor=current_user)
    except project_service.ProjectError as exc:
        flash(str(exc), "error")
    else:
        flash(f"{member.display_name} removed from the project.", "info")
    return redirect(url_for("projects.detail", project_id=project.id) + "#team")


@bp.route("/<int:project_id>/milestones", methods=["POST"])
@login_required
def add_milestone(project_id: int):
    project = _get_project_or_404(project_id)
    try:
        task_service.create_milestone(
            actor=current_user,
            project=project,
            title=request.form.get("title", ""),
            description=request.form.get("description"),
            due_date=parse_date(request.form.get("due_date"), field="Milestone date"),
        )
    except (task_service.TaskError, ValidationError) as exc:
        flash(str(exc), "error")
    else:
        flash("Milestone added.", "success")
    return redirect(url_for("projects.detail", project_id=project.id) + "#milestones")


@bp.route("/milestones/<int:milestone_id>/complete", methods=["POST"])
@login_required
def complete_milestone(milestone_id: int):
    milestone = db.session.get(Milestone, milestone_id)
    if milestone is None or not can_view_project(current_user, milestone.project):
        abort(404)

    try:
        task_service.complete_milestone(milestone, actor=current_user)
    except task_service.TaskError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Milestone “{milestone.title}” completed.", "success")
    return redirect(url_for("projects.detail", project_id=milestone.project_id) + "#milestones")


@bp.route("/<int:project_id>/archive", methods=["POST"])
@login_required
def archive(project_id: int):
    project = _get_project_or_404(project_id)
    try:
        project_service.archive_project(project, actor=current_user)
    except project_service.ProjectError as exc:
        flash(str(exc), "error")
    else:
        flash("Project archived. Records and history are retained.", "info")
    return redirect(url_for("projects.index"))
