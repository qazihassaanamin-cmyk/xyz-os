"""Team directory and the personal founder profile.

The profile is the professional record of a member inside Tavryc: identity and
official role, contribution and XP progress, system achievements, CEO-granted
recognition, focus history and a chronological contribution history. Account
secrets never appear here — there is no code path that renders a password hash
or a token.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.founders import FOUNDERS, founder_by_identifier
from app.models.project import Project, ProjectMember, ProjectRole
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.permissions import can_view_xp_history, is_ceo, login_required
from app.services import (
    achievement_service,
    activity_service,
    badge_service,
    focus_service,
    report_service,
    xp_service,
)
from app.utils.dates import company_today, deadline_label, humanise_seconds
from app.utils.validators import nulls_last_asc

bp = Blueprint("team", __name__)


def _get_member_or_404(username: str) -> User:
    """Any signed-in member may view a colleague's professional profile."""
    user = User.query.filter(db.func.lower(User.username) == (username or "").lower()).first()
    if user is None or not user.is_active:
        abort(404)
    return user


def founder_meta(user) -> dict | None:
    """The official role description for this member, when they are a founder."""
    return founder_by_identifier(user.email) or founder_by_identifier(user.username)


@bp.route("/")
@login_required
def index():
    """Company directory. Every active member, with their current load."""
    members = User.query.filter_by(is_active_flag=True).order_by(User.role.asc(), User.full_name.asc()).all()
    rows = []
    for member in members:
        profile = xp_service.ensure_profile(member)
        rows.append(
            {
                "user": member,
                "profile": profile,
                "meta": founder_meta(member),
                "workload": report_service.member_workload(member),
                "badges": badge_service.active_for(member)[:3],
                "achievement_count": len(achievement_service.for_user(member)),
            }
        )

    return render_template(
        "team/index.html",
        page_title="Team members",
        rows=rows,
        is_ceo=is_ceo(current_user),
        deadline_label=deadline_label,
    )


@bp.route("/<username>")
@login_required
def profile(username: str):
    """A member's profile. Founders also see their own edit controls."""
    user = _get_member_or_404(username)
    is_self = user.id == current_user.id

    tab = request.args.get("tab", "overview")
    if tab not in {"overview", "work", "achievements", "recognition", "focus", "activity"}:
        tab = "overview"

    profile = xp_service.ensure_profile(user)
    meta = founder_meta(user)

    context = {
        "page_title": user.display_name,
        "member": user,
        "profile": profile,
        "xp_profile": profile,
        "meta": meta,
        "is_self": is_self,
        "is_ceo": is_ceo(current_user),
        "tab": tab,
        "xp_history": xp_service.history_for(user, limit=25),
        "achievements": achievement_service.for_user(user),
        "badges": badge_service.all_for(user, include_revoked=is_ceo(current_user)),
        "active_badges": badge_service.active_for(user),
        "workload": report_service.member_workload(user),
        "weekly": activity_service.weekly_progress(user),
        "quality": activity_service.quality_indicators(user),
        "focus_stats": focus_service.stats_for(user, days=90),
        "focus_daily": focus_service.daily_summary(user, days=21),
        "focus_sessions": focus_service.history_for(user, limit=20),
        "history": activity_service.contribution_history(user, days=180, limit=60),
        "can_view_xp": can_view_xp_history(current_user, user),
        "humanise_seconds": humanise_seconds,
        "all_founders": [
            User.query.filter(
                db.or_(User.email == spec["identifier"], User.username == spec["username"])
            ).first()
            for spec in FOUNDERS
        ],
        "projects": _member_projects(user),
        "open_tasks": _member_open_tasks(user),
    }
    return render_template("team/profile.html", **context)


def _member_projects(user):
    return (
        ProjectMember.query.filter_by(user_id=user.id)
        .join(ProjectMember.project)
        .order_by(Project.name.asc())
        .all()
    )


def _member_open_tasks(user, limit: int = 10):
    return (
        Task.query.filter(
            Task.assignee_id == user.id,
            Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
        )
        .order_by(*nulls_last_asc(Task.due_date))
        .limit(limit)
        .all()
    )


@bp.route("/<username>/edit", methods=["POST"])
@login_required
def edit_profile(username: str):
    """A member may edit their own biography and skills.

    Official role and title are not editable here: those are company records the
    CEO maintains, so the permission boundary is enforced by which fields this
    route will accept at all.
    """
    user = _get_member_or_404(username)
    if user.id != current_user.id and not is_ceo(current_user):
        abort(403)

    bio = (request.form.get("bio") or "").strip()[:2000] or None
    skills = (request.form.get("skills") or "").strip()[:1000] or None
    timezone = (request.form.get("timezone") or "").strip()[:64] or None
    full_name = (request.form.get("full_name") or "").strip()[:160]

    before = {"bio": user.bio, "skills": user.skills, "timezone": user.timezone, "full_name": user.full_name}

    user.bio = bio
    user.skills = skills
    if timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            flash("That time zone is not recognised.", "error")
            return redirect(url_for("team.profile", username=user.username))
        user.timezone = timezone
    if full_name and len(full_name) >= 2:
        user.full_name = full_name

    # Only the CEO may change the official role and title.
    if is_ceo(current_user) and user.id != current_user.id:
        title = (request.form.get("title") or "").strip()[:160] or None
        user.title = title
        functions = (request.form.get("functions") or "").strip()[:2000] or None
        user.functions = functions
    db.session.commit()

    from app.services.audit_service import record_audit

    record_audit(
        "profile.updated",
        entity_type="user",
        entity_id=user.id,
        summary=f"Profile details updated for {user.username}",
        before=before,
        after={"bio": user.bio, "skills": user.skills, "timezone": user.timezone, "title": user.title},
        user_id=current_user.id,
    )
    flash("Profile updated.", "success")
    return redirect(url_for("team.profile", username=user.username, tab="overview"))


# ---------------------------------------------------------------------------
# CEO-granted recognition
# ---------------------------------------------------------------------------

@bp.route("/<username>/badges", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def award_badge(username: str):
    """Grant a recognition badge or title. CEO only, enforced server-side."""
    if not is_ceo(current_user):
        abort(403)

    user = _get_member_or_404(username)
    project_id = request.form.get("project_id", type=int)

    try:
        badge_service.award_badge(
            user,
            name=request.form.get("name", ""),
            description=request.form.get("description"),
            actor=current_user,
            project_id=project_id,
        )
    except badge_service.BadgeError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Recognition awarded to {user.display_name}.", "success")
    return redirect(url_for("team.profile", username=user.username, tab="recognition"))


@bp.route("/badges/<int:badge_id>/edit", methods=["POST"])
@login_required
def edit_badge(badge_id: int):
    if not is_ceo(current_user):
        abort(403)

    from app.models.achievement import MemberBadge

    badge = db.session.get(MemberBadge, badge_id)
    if badge is None:
        abort(404)

    try:
        badge_service.update_badge(
            badge,
            name=request.form.get("name", ""),
            description=request.form.get("description"),
            actor=current_user,
        )
    except badge_service.BadgeError as exc:
        flash(str(exc), "error")
    else:
        flash("Recognition updated.", "success")
    return redirect(url_for("team.profile", username=badge.user.username, tab="recognition"))


@bp.route("/badges/<int:badge_id>/revoke", methods=["POST"])
@login_required
def revoke_badge(badge_id: int):
    """Revoke recognition while keeping the historical record."""
    if not is_ceo(current_user):
        abort(403)

    from app.models.achievement import MemberBadge

    badge = db.session.get(MemberBadge, badge_id)
    if badge is None:
        abort(404)

    try:
        badge_service.revoke_badge(
            badge, reason=request.form.get("reason", ""), actor=current_user
        )
    except badge_service.BadgeError as exc:
        flash(str(exc), "error")
    else:
        flash("Recognition revoked. The record is retained.", "info")
    return redirect(url_for("team.profile", username=badge.user.username, tab="recognition"))


@bp.route("/badges/<int:badge_id>/reactivate", methods=["POST"])
@login_required
def reactivate_badge(badge_id: int):
    if not is_ceo(current_user):
        abort(403)

    from app.models.achievement import MemberBadge

    badge = db.session.get(MemberBadge, badge_id)
    if badge is None:
        abort(404)

    badge_service.reactivate_badge(badge, actor=current_user)
    flash("Recognition reinstated.", "success")
    return redirect(url_for("team.profile", username=badge.user.username, tab="recognition"))


# ---------------------------------------------------------------------------
# CEO: account status and role changes
# ---------------------------------------------------------------------------

@bp.route("/<username>/account", methods=["POST"])
@login_required
def update_account(username: str):
    """Change a member's official role, title or active status. CEO only."""
    if not is_ceo(current_user):
        abort(403)

    user = _get_member_or_404(username)
    if user.id == current_user.id:
        flash("Use your own settings page for your account.", "error")
        return redirect(url_for("team.profile", username=user.username))

    before = {
        "role": user.role,
        "title": user.title,
        "is_active": user.is_active_flag,
    }

    action = request.form.get("action")

    if action == "deactivate":
        user.is_active_flag = False
        from app.models.base import utcnow

        user.deactivated_at = utcnow()
        message = f"{user.display_name} deactivated. Their sessions end on the next request."
    elif action == "reactivate":
        user.is_active_flag = True
        user.deactivated_at = None
        message = f"{user.display_name} reactivated."
    elif action == "set_title":
        user.title = (request.form.get("title") or "").strip()[:160] or None
        message = f"Official title updated for {user.display_name}."
    elif action == "set_role":
        new_role = (request.form.get("role") or "").strip().upper()
        if new_role not in ("CEO", "MEMBER"):
            flash("Choose a valid role.", "error")
            return redirect(url_for("team.profile", username=user.username))
        if new_role == "MEMBER" and User.query.filter_by(role="CEO", is_active_flag=True).count() <= 1:
            flash("There must always be at least one active CEO.", "error")
            return redirect(url_for("team.profile", username=user.username))
        user.role = new_role
        message = f"{user.display_name} is now {new_role}."
    else:
        flash("Unknown action.", "error")
        return redirect(url_for("team.profile", username=user.username))

    db.session.commit()

    from app.services.audit_service import record_audit

    record_audit(
        "user.account_updated",
        entity_type="user",
        entity_id=user.id,
        summary=message,
        before=before,
        after={
            "role": user.role,
            "title": user.title,
            "is_active": user.is_active_flag,
        },
        user_id=current_user.id,
    )
    flash(message, "success")
    return redirect(url_for("team.profile", username=user.username))


@bp.route("/<username>/reset-link", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def generate_reset_link(username: str):
    """Produce a single-use reset link for a member. CEO only.

    The link is shown once on the next page load and is never stored in
    readable form; the database only holds its hash.
    """
    if not is_ceo(current_user):
        abort(403)

    user = _get_member_or_404(username)
    from app.services.auth_service import create_reset_token
    from app.services.mail_service import send_reset_email

    raw_token = create_reset_token(user, requested_ip=request.remote_addr)
    link = url_for("auth.reset_password", token=raw_token, _external=True)
    sent = send_reset_email(user, link)

    flash(
        f"Reset link generated for {user.display_name}. "
        + ("It was emailed." if sent else "SMTP is not configured, so the link is shown once below."),
        "info",
    )
    if not sent:
        # Shown exactly once, in this response only.
        flash(f"One-time reset link: {link}", "warning")
    return redirect(url_for("team.profile", username=user.username))
