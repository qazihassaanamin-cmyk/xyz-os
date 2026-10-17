"""Server-side authorization.

Every protected route calls a helper from this module. Hiding a navigation link
is presentation only and is never treated as a permission check. The helpers
return booleans; routes turn a ``False`` into a 403.
"""

from __future__ import annotations

from functools import wraps

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user

from app.models.project import Project, ProjectRole
from app.models.task import Blocker, Task, TaskStatus, Submission, SubmissionStatus


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _user(user=None):
    return user if user is not None else current_user


def is_authenticated(user=None) -> bool:
    candidate = _user(user)
    return bool(candidate and getattr(candidate, "is_authenticated", False))


def is_ceo(user=None) -> bool:
    candidate = _user(user)
    return bool(is_authenticated(candidate) and candidate.role == "CEO")


def is_member_of_company(user=None) -> bool:
    """Every active account belongs to the company; there are no guests."""
    candidate = _user(user)
    return bool(is_authenticated(candidate) and candidate.is_active)


# ---------------------------------------------------------------------------
# Project / task scoping
# ---------------------------------------------------------------------------

def project_role(user, project: Project | None) -> str | None:
    if project is None or user is None:
        return None
    for membership in project.members:
        if membership.user_id == user.id:
            return membership.role_in_project
    return None


def is_project_lead(user, project: Project | None) -> bool:
    return project_role(user, project) == ProjectRole.LEAD.value


def is_project_member(user, project: Project | None) -> bool:
    return project_role(user, project) is not None


def can_view_project(user, project: Project | None) -> bool:
    if project is None:
        return False
    return is_ceo(user) or is_project_member(user, project)


def can_manage_project(user, project: Project | None) -> bool:
    """Rename, re-scope, archive, add or remove members, set milestones."""
    if project is None:
        return False
    return is_ceo(user) or is_project_lead(user, project)


def can_create_project(user) -> bool:
    """Only the CEO creates company projects, per the brief."""
    return is_ceo(user)


def can_view_task(user, task: Task | None) -> bool:
    if task is None:
        return False
    if is_ceo(user):
        return True
    if task.assignee_id == getattr(user, "id", None):
        return True
    if task.creator_id == getattr(user, "id", None):
        return True
    if task.reviewer_id == getattr(user, "id", None):
        return True
    return can_view_project(user, task.project)


def can_edit_task(user, task: Task | None) -> bool:
    """Full task editing, including reassignment and difficulty."""
    if task is None:
        return False
    if is_ceo(user):
        return True
    if is_project_lead(user, task.project):
        return True
    # The assignee may edit descriptive fields only; see can_edit_task_details.
    return False


def can_edit_task_details(user, task: Task | None) -> bool:
    """Assignee-editable fields: description, progress note, due-date request."""
    if task is None:
        return False
    if can_edit_task(user, task):
        return True
    return task.assignee_id == getattr(user, "id", None) and task.is_open


def can_set_task_difficulty(user, task: Task | None) -> bool:
    """Difficulty and base XP are set by the CEO or an authorized lead only."""
    if task is None:
        return False
    if is_ceo(user):
        return True
    return is_project_lead(user, task.project)


def can_assign_task(user, project: Project | None) -> bool:
    if is_ceo(user):
        return True
    return is_project_lead(user, project)


def can_delete_task(user, task: Task | None) -> bool:
    if task is None:
        return False
    if is_ceo(user):
        return True
    return is_project_lead(user, task.project) and task.status in (
        TaskStatus.BACKLOG.value,
        TaskStatus.TODO.value,
    )


def can_change_task_status(user, task: Task | None, target_status: str | None = None) -> bool:
    """Status changes follow the state machine in ``task_service``."""
    if task is None:
        return False
    if is_ceo(user):
        return True
    if task.assignee_id == getattr(user, "id", None):
        return True
    if is_project_lead(user, task.project):
        return True
    return False


def can_acknowledge_task(user, task: Task | None) -> bool:
    if task is None:
        return False
    return task.can_be_acknowledged_by(user)


def can_submit_work(user, task: Task | None) -> bool:
    """Only the assignee submits work, and only while the task is open."""
    if task is None:
        return False
    if task.assignee_id != getattr(user, "id", None):
        return False
    return task.status in (
        TaskStatus.TODO.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.IN_REVIEW.value,
    )


def can_review_submission(user, submission: Submission | None) -> bool:
    """Reviewers are the CEO or the designated reviewer; never the author."""
    if submission is None:
        return False
    task = submission.task
    if task is None:
        return False
    if submission.submitted_by_id == getattr(user, "id", None):
        return False
    if submission.status != SubmissionStatus.PENDING.value:
        return False
    if is_ceo(user):
        return True
    if task.reviewer_id == getattr(user, "id", None):
        return True
    return False


def can_raise_blocker(user, task: Task | None) -> bool:
    if task is None:
        return False
    if task.assignee_id == getattr(user, "id", None):
        return True
    if is_ceo(user):
        return True
    return is_project_lead(user, task.project)


def can_resolve_blocker(user, blocker: Blocker | None) -> bool:
    if blocker is None:
        return False
    if is_ceo(user):
        return True
    if blocker.raised_by_id == getattr(user, "id", None):
        return True
    if blocker.assigned_to_id == getattr(user, "id", None):
        return True
    task = blocker.task
    if task is not None and is_project_lead(user, task.project):
        return True
    return False


def can_help_with_blocker(user, blocker: Blocker | None) -> bool:
    """Any member except the person who raised it may step in to help."""
    if blocker is None or not is_member_of_company(user):
        return False
    return blocker.raised_by_id != getattr(user, "id", None)


def can_view_blocker(user, blocker: Blocker | None) -> bool:
    if blocker is None:
        return False
    if is_ceo(user):
        return True
    if blocker.raised_by_id == getattr(user, "id", None):
        return True
    if blocker.assigned_to_id == getattr(user, "id", None):
        return True
    return can_view_project(user, blocker.project)


# ---------------------------------------------------------------------------
# Gamification administration
# ---------------------------------------------------------------------------

def can_manage_xp_rules(user) -> bool:
    """Only the CEO changes XP values, levels, badges or challenges."""
    return is_ceo(user)


def can_award_xp(user, target_user=None) -> bool:
    """Manual XP adjustments are CEO-only and never self-awarded."""
    if not is_ceo(user):
        return False
    if target_user is not None and getattr(target_user, "id", None) == getattr(user, "id", None):
        return False
    return True


def can_view_xp_history(user, target_user=None) -> bool:
    if is_ceo(user):
        return True
    if target_user is None:
        return is_member_of_company(user)
    return getattr(target_user, "id", None) == getattr(user, "id", None)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def _safe_next_target() -> str:
    """Only ever redirect back into this application."""
    target = request.full_path if request.method == "GET" else None
    target = target or request.path
    if not target.startswith("/") or target.startswith("//"):
        return url_for("dashboard.index")
    return target


def login_required(view):
    """Require an authenticated, active member; otherwise send to /login."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            flash("Please sign in to continue.", "info")
            return redirect(url_for("auth.login", next=_safe_next_target()))
        if not current_user.is_active:
            from flask_login import logout_user

            logout_user()
            flash("This account is not active. Contact the administrator.", "error")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapper


def roles_required(*roles: str):
    """Restrict a view to the given company roles."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not is_authenticated():
                flash("Please sign in to continue.", "info")
                return redirect(url_for("auth.login", next=_safe_next_target()))
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def ceo_required(view):
    """Restrict a view to the CEO."""
    return roles_required("CEO")(view)


def password_change_required(view):
    """Force a password change before anything else when the flag is set."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_authenticated() and current_user.must_change_password:
            from flask import url_for as _url_for

            endpoint = request.endpoint or ""
            if endpoint not in {
                "auth.change_password",
                "auth.logout",
                "auth.login",
                "static",
            }:
                flash("Set a new password before continuing.", "info")
                return redirect(_url_for("auth.change_password"))
        return view(*args, **kwargs)

    return wrapper
