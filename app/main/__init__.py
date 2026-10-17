"""Root redirect and small shared routes."""

from __future__ import annotations

from flask import Blueprint, redirect, url_for
from flask_login import current_user

from app.permissions import login_required

bp = Blueprint("main", __name__)


@bp.route("/")
def root():
    """Send a signed-in member to the right landing page for their role."""
    if not current_user.is_authenticated or not current_user.is_active:
        return redirect(url_for("auth.login"))

    if current_user.is_ceo:
        return redirect(url_for("dashboard.ceo"))

    # A member who leads at least one project lands on their projects; the CEO
    # landing is reserved for the CEO so the command centre is not diluted.
    from app.models.project import ProjectMember, ProjectRole

    leads_something = (
        ProjectMember.query.filter_by(
            user_id=current_user.id, role_in_project=ProjectRole.LEAD.value
        ).first()
        is not None
    )
    if leads_something:
        return redirect(url_for("projects.index"))
    return redirect(url_for("dashboard.index"))


@bp.route("/robots.txt")
def robots():
    """This is a private application; it must never be indexed."""
    return (
        "User-agent: *\nDisallow: /\n",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
    )
