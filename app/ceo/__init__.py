"""CEO-only management surfaces: reports, the audit trail and settings.

Every route here is gated by ``ceo_required``. Hiding the navigation entry is a
presentation detail; the decorator is the enforcement.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.audit import AuditLog
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.permissions import ceo_required, login_required
from app.services import report_service, settings_service
from app.utils.dates import company_today
from app.utils.pagination import paginate
from app.utils.validators import parse_date

bp = Blueprint("ceo", __name__)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@bp.route("/reports")
@login_required
@ceo_required
def reports():
    """Weekly and monthly figures. Each metric stays in its own column."""
    period = request.args.get("period", "month")
    today = company_today()

    if period == "week":
        start, end = report_service.parse_range(None, None, default_days=7)
    elif period == "quarter":
        start, end = report_service.parse_range(None, None, default_days=90)
    else:
        period = "month"
        start, end = report_service.parse_range(None, None, default_days=30)

    members = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()

    return render_template(
        "ceo/reports.html",
        page_title="Reports",
        period=period,
        start=start,
        end=end,
        today=today,
        task_metrics=report_service.task_metrics(start=start, end=end),
        review_metrics=report_service.review_metrics(start=start, end=end),
        focus_metrics=report_service.focus_metrics(start=start, end=end),
        reporting_metrics=report_service.reporting_metrics(start=start, end=end),
        compliance=report_service.compliance_by_member(start=start, end=end, members=members),
        weekly=report_service.weekly_digest(start=today),
    )


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@bp.route("/activity")
@login_required
@ceo_required
def activity():
    """The append-only audit trail, filterable and paginated."""
    actor_id = request.args.get("actor", type=int)
    action = request.args.get("action", "")
    entity_type = request.args.get("entity_type", "")
    start = parse_date(request.args.get("start"), field="Start date")
    end = parse_date(request.args.get("end"), field="End date")

    query = AuditLog.query

    if actor_id:
        query = query.filter(AuditLog.user_id == actor_id)
    if action:
        query = query.filter(AuditLog.action.ilike(f"%{action}%"))
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if start:
        query = query.filter(db.func.date(AuditLog.created_at) >= start)
    if end:
        query = query.filter(db.func.date(AuditLog.created_at) <= end)

    query = query.order_by(AuditLog.created_at.desc())
    pagination = paginate(query, per_page=50)

    actions = [
        row[0]
        for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
    ]
    entity_types = [
        row[0]
        for row in db.session.query(AuditLog.entity_type)
        .distinct()
        .order_by(AuditLog.entity_type)
        .all()
        if row[0]
    ]

    return render_template(
        "ceo/activity.html",
        page_title="Activity log",
        pagination=pagination,
        entries=pagination.items,
        members=User.query.order_by(User.full_name).all(),
        actions=actions,
        entity_types=entity_types,
        selected_actor=actor_id,
        selected_action=action,
        selected_entity=entity_type,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@login_required
@ceo_required
@limiter.limit("60 per hour", methods=["POST"])
def settings():
    """Company, gamification, storage and daily-record settings."""
    if request.method == "POST":
        updated = 0
        errors = []

        for row in settings_service.grouped().values():
            for setting in row:
                if setting.is_secret:
                    continue
                field = f"setting_{setting.key}"
                if field not in request.form and f"{field}_present" not in request.form:
                    # Checkboxes are absent when unchecked, so boolean settings
                    # are always rendered with a companion marker field.
                    if setting.value_type == "bool":
                        settings_service.set_value(setting.key, False, actor=current_user)
                        updated += 1
                    continue

                if setting.value_type == "bool":
                    settings_service.set_value(
                        setting.key, request.form.get(field) == "1", actor=current_user
                    )
                    updated += 1
                    continue

                raw = (request.form.get(field) or "").strip()
                if setting.value_type == "int":
                    try:
                        settings_service.set_value(setting.key, int(raw), actor=current_user)
                    except ValueError:
                        errors.append(f"“{setting.label or setting.key}” must be a whole number.")
                        continue
                else:
                    settings_service.set_value(setting.key, raw or None, actor=current_user)
                updated += 1

        if errors:
            for message in errors:
                flash(message, "error")
        if updated:
            flash(f"{updated} setting(s) saved. Changes apply from the next request.", "success")

        # Operational values are re-read from the database, so push them now.
        settings_service.apply_runtime_overrides(db.get_app())
        return redirect(url_for("ceo.settings"))

    return render_template(
        "ceo/settings.html",
        page_title="Settings",
        groups=settings_service.grouped(),
        category_labels=settings_service.CATEGORY_LABELS,
        storage=_storage(),
    )


def _storage() -> dict | None:
    try:
        from app.services.upload_service import storage_usage

        return storage_usage()
    except Exception:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Team overview used by the CEO's member list
# ---------------------------------------------------------------------------

@bp.route("/members")
@login_required
@ceo_required
def members():
    """Dense table of every member with the figures the CEO actually needs."""
    members_list = User.query.order_by(User.role.asc(), User.full_name.asc()).all()
    start, end = report_service.parse_range(None, None, default_days=30)

    rows = []
    for member in members_list:
        from app.services import achievement_service, xp_service

        rows.append(
            {
                "user": member,
                "profile": xp_service.ensure_profile(member),
                "workload": report_service.member_workload(member),
                "achievements": len(achievement_service.for_user(member)),
                "tasks": report_service.task_metrics(start=start, end=end, member=member),
                "reviews": report_service.review_metrics(start=start, end=end, member=member),
                "focus": report_service.focus_metrics(start=start, end=end, member=member),
                "reporting": report_service.reporting_metrics(start=start, end=end, member=member),
            }
        )

    return render_template(
        "ceo/members.html",
        page_title="Team members",
        rows=rows,
        start=start,
        end=end,
    )
