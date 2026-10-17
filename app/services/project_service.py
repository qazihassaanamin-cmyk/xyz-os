"""Projects and membership."""

from __future__ import annotations

import logging

from app.extensions import db
from app.models.base import utcnow
from app.models.project import (
    Milestone,
    MilestoneStatus,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectStatus,
)
from app.models.task import Task, TaskStatus
from app.services.audit_service import record_audit
from app.utils.validators import ValidationError, optional_text, parse_slug, require_text

log = logging.getLogger(__name__)


class ProjectError(Exception):
    """A refused project operation, with a member-safe message."""


def create_project(
    *,
    actor,
    name: str,
    description: str | None = None,
    objectives: str | None = None,
    status: str = ProjectStatus.PLANNING.value,
    owner=None,
    start_date=None,
    target_date=None,
    members: list | None = None,
) -> Project:
    from app.permissions import can_create_project

    if not can_create_project(actor):
        raise ProjectError("Only the CEO can create a project.")

    name = require_text(name, field="Project name", min_length=2, max_length=160)
    status_value = ProjectStatus.coerce(status)
    if status_value is None:
        raise ProjectError("Choose a valid project status.")
    if start_date and target_date and target_date < start_date:
        raise ProjectError("The target date cannot be before the start date.")

    slug = _unique_slug(parse_slug(name))

    project = Project(
        name=name,
        slug=slug,
        description=optional_text(description),
        objectives=optional_text(objectives),
        status=status_value.value,
        owner_id=getattr(owner, "id", None) or actor.id,
        created_by_id=actor.id,
        start_date=start_date,
        target_date=target_date,
    )
    db.session.add(project)
    db.session.flush()

    # The owner is always a member, and the CEO always retains oversight.
    member_ids = {actor.id}
    if owner is not None:
        member_ids.add(owner.id)
    for member in members or []:
        member_ids.add(member.id)

    for member_id in member_ids:
        role = (
            ProjectRole.LEAD.value
            if member_id == (project.owner_id or actor.id)
            else ProjectRole.MEMBER.value
        )
        db.session.add(
            ProjectMember(project_id=project.id, user_id=member_id, role_in_project=role)
        )

    db.session.commit()

    record_audit(
        "project.created",
        entity_type="project",
        entity_id=project.id,
        summary=f"Project “{project.name}” created",
        after={
            "name": project.name,
            "status": project.status,
            "owner_id": project.owner_id,
            "member_ids": sorted(member_ids),
        },
        user_id=actor.id,
    )
    return project


def _unique_slug(base: str) -> str:
    slug = base
    suffix = 2
    while Project.query.filter_by(slug=slug).first() is not None:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def update_project(project: Project, *, actor, **fields) -> Project:
    from app.permissions import can_manage_project

    if not can_manage_project(actor, project):
        raise ProjectError("You do not have permission to edit this project.")

    before: dict = {}
    after: dict = {}

    def record(key, value):
        old = getattr(project, key, None)
        if old != value:
            before[key] = old.isoformat() if hasattr(old, "isoformat") else old
            after[key] = value.isoformat() if hasattr(value, "isoformat") else value
            setattr(project, key, value)

    if "name" in fields and fields["name"] is not None:
        record("name", require_text(fields["name"], field="Project name", min_length=2, max_length=160))
    if "description" in fields:
        record("description", optional_text(fields["description"]))
    if "objectives" in fields:
        record("objectives", optional_text(fields["objectives"]))
    if "status" in fields and fields["status"] is not None:
        status_value = ProjectStatus.coerce(fields["status"])
        if status_value is None:
            raise ProjectError("Choose a valid project status.")
        record("status", status_value.value)
        if status_value == ProjectStatus.COMPLETED and project.completed_at is None:
            project.completed_at = utcnow()
        if status_value == ProjectStatus.ARCHIVED and project.archived_at is None:
            project.archived_at = utcnow()
    if "start_date" in fields:
        record("start_date", fields["start_date"])
    if "target_date" in fields:
        record("target_date", fields["target_date"])
    if "owner_id" in fields and fields["owner_id"] is not None:
        record("owner_id", int(fields["owner_id"]))

    if project.start_date and project.target_date and project.target_date < project.start_date:
        raise ProjectError("The target date cannot be before the start date.")

    if not after:
        return project

    db.session.commit()
    record_audit(
        "project.updated",
        entity_type="project",
        entity_id=project.id,
        summary=f"Project “{project.name}” updated",
        before=before,
        after=after,
        user_id=actor.id,
    )
    return project


def add_member(project: Project, *, member, actor, role_in_project: str = ProjectRole.MEMBER.value) -> ProjectMember:
    from app.permissions import can_manage_project

    if not can_manage_project(actor, project):
        raise ProjectError("You do not have permission to change project membership.")
    if member is None or not member.is_active:
        raise ProjectError("Choose an active member.")

    role_value = ProjectRole.coerce(role_in_project) or ProjectRole.MEMBER
    if role_value.value == ProjectRole.LEAD.value and not getattr(actor, "is_ceo", False):
        raise ProjectError("Only the CEO can appoint a project lead.")

    existing = ProjectMember.query.filter_by(project_id=project.id, user_id=member.id).first()
    if existing is not None:
        if existing.role_in_project != role_value.value:
            existing.role_in_project = role_value.value
            db.session.commit()
            record_audit(
                "project.member_role_changed",
                entity_type="project",
                entity_id=project.id,
                summary=f"{member.display_name} is now {role_value.value.title()} on {project.name}",
                user_id=actor.id,
            )
        return existing

    row = ProjectMember(
        project_id=project.id, user_id=member.id, role_in_project=role_value.value
    )
    db.session.add(row)
    db.session.commit()

    record_audit(
        "project.member_added",
        entity_type="project",
        entity_id=project.id,
        summary=f"{member.display_name} added to {project.name} as {role_value.value.title()}",
        after={"user_id": member.id, "role": role_value.value},
        user_id=actor.id,
    )
    try:
        from app.services.notification_service import notify

        notify(
            member,
            "ACCOUNT_CHANGED",
            title=f"You were added to {project.name}",
            body=f"Your role on this project is {role_value.value.title()}.",
            link_endpoint="projects.detail",
            link_params=f"project_id={project.id}",
            subject_type="project",
            subject_id=project.id,
            dedupe_key=f"project-member:{project.id}:{member.id}",
            actor=actor,
        )
    except Exception:  # pragma: no cover
        log.exception("Project membership notification failed")
    return row


def remove_member(project: Project, *, member, actor) -> None:
    from app.permissions import can_manage_project

    if not can_manage_project(actor, project):
        raise ProjectError("You do not have permission to change project membership.")
    if project.owner_id == member.id:
        raise ProjectError(
            "Reassign the project owner before removing them from the project."
        )

    open_tasks = Task.query.filter(
        Task.project_id == project.id,
        Task.assignee_id == member.id,
        Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
    ).count()
    if open_tasks:
        raise ProjectError(
            f"{member.display_name} still has {open_tasks} open task(s) in this project. "
            "Reassign them first."
        )

    row = ProjectMember.query.filter_by(project_id=project.id, user_id=member.id).first()
    if row is None:
        raise ProjectError("That member is not on this project.")

    db.session.delete(row)
    db.session.commit()

    record_audit(
        "project.member_removed",
        entity_type="project",
        entity_id=project.id,
        summary=f"{member.display_name} removed from {project.name}",
        before={"user_id": member.id},
        user_id=actor.id,
    )


def archive_project(project: Project, *, actor) -> Project:
    from app.permissions import can_manage_project

    if not can_manage_project(actor, project):
        raise ProjectError("You do not have permission to archive this project.")
    if not getattr(actor, "is_ceo", False):
        raise ProjectError("Only the CEO can archive a project.")

    open_tasks = Task.query.filter(
        Task.project_id == project.id,
        Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
    ).count()
    if open_tasks:
        raise ProjectError(
            f"{open_tasks} task(s) are still open. Complete or cancel them before archiving."
        )

    project.status = ProjectStatus.ARCHIVED.value
    project.archived_at = utcnow()
    db.session.commit()

    record_audit(
        "project.archived",
        entity_type="project",
        entity_id=project.id,
        summary=f"Project “{project.name}” archived",
        after={"status": project.status},
        user_id=actor.id,
    )
    return project


def progress_for(project: Project) -> dict:
    """Delivery progress for a project, computed from its tasks."""
    tasks = Task.query.filter_by(project_id=project.id).all()
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED.value)
    cancelled = sum(1 for t in tasks if t.status == TaskStatus.CANCELLED.value)
    active = total - done - cancelled
    in_review = sum(1 for t in tasks if t.status == TaskStatus.IN_REVIEW.value)
    blocked = sum(1 for t in tasks if t.status == TaskStatus.BLOCKED.value)
    countable = max(1, total - cancelled)

    milestones = project.milestones
    milestone_done = sum(1 for m in milestones if m.status == MilestoneStatus.COMPLETED.value)

    return {
        "total_tasks": total,
        "completed": done,
        "active": active,
        "in_review": in_review,
        "blocked": blocked,
        "cancelled": cancelled,
        "percent": round(100 * done / countable),
        "milestones_total": len(milestones),
        "milestones_done": milestone_done,
        "is_overdue": bool(
            project.target_date
            and project.target_date < utcnow().date()
            and project.is_open
        ),
    }


def visible_projects_query(user, *, include_archived: bool = False):
    """Projects this member may see, filtered at the query level."""
    from app.permissions import is_ceo

    query = Project.query
    if not is_ceo(user):
        query = query.join(
            ProjectMember, ProjectMember.project_id == Project.id
        ).filter(ProjectMember.user_id == user.id)
    if not include_archived:
        query = query.filter(Project.status != ProjectStatus.ARCHIVED.value)
    return query.order_by(Project.status.asc(), Project.name.asc())


def membership_rows(project: Project) -> list[dict]:
    """Members with their task load, for the project detail page."""
    rows = []
    for membership in project.members:
        open_tasks = Task.query.filter(
            Task.project_id == project.id,
            Task.assignee_id == membership.user_id,
            Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
        ).count()
        done_tasks = Task.query.filter(
            Task.project_id == project.id,
            Task.assignee_id == membership.user_id,
            Task.status == TaskStatus.COMPLETED.value,
        ).count()
        rows.append(
            {
                "membership": membership,
                "user": membership.user,
                "open_tasks": open_tasks,
                "done_tasks": done_tasks,
            }
        )
    rows.sort(key=lambda r: (not r["membership"].is_lead, r["user"].full_name or ""))
    return rows


def summary_for_user(user) -> list[dict]:
    """Compact project cards for the member's own project list."""
    rows = []
    for project in visible_projects_query(user).all():
        progress = progress_for(project)
        my_open = Task.query.filter(
            Task.project_id == project.id,
            Task.assignee_id == user.id,
            Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
        ).count()
        rows.append(
            {
                "project": project,
                "progress": progress,
                "my_open_tasks": my_open,
                "role": project.role_of(user),
            }
        )
    return rows
