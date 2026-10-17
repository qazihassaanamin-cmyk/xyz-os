"""Daily check-in and end-of-day report."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.daily import DailyCheckin, DailyReport
from app.models.user import User
from app.permissions import is_ceo, login_required
from app.services import daily_service, report_service, workday_service
from app.utils.dates import company_today
from app.utils.pagination import paginate
from app.utils.validators import parse_date

bp = Blueprint("daily", __name__)


@bp.route("/")
@login_required
def index():
    """Today's check-in and report, plus a short recent history."""
    status = daily_service.today_status(current_user)
    history = daily_service.history_for(current_user, limit=14)

    return render_template(
        "daily/index.html",
        page_title="Daily check-in",
        status=status,
        history=history,
        today=company_today(),
    )


@bp.route("/checkin", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def save_checkin():
    try:
        daily_service.save_checkin(
            current_user,
            priorities=request.form.get("priorities", ""),
            intended_result=request.form.get("intended_result"),
            support_needed=request.form.get("support_needed"),
            blockers=request.form.get("blockers"),
        )
    except (daily_service.DailyError, ValueError) as exc:
        flash(str(exc), "error")
    else:
        flash("Check-in saved for today.", "success")
    return redirect(url_for("daily.index") + "#checkin")


@bp.route("/report", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def save_report():
    try:
        daily_service.save_report(
            current_user,
            accomplished=request.form.get("accomplished", ""),
            submitted_summary=request.form.get("submitted_summary"),
            unfinished=request.form.get("unfinished"),
            unfinished_reason=request.form.get("unfinished_reason"),
            next_steps=request.form.get("next_steps"),
        )
    except (daily_service.DailyError, ValueError) as exc:
        flash(str(exc), "error")
    else:
        flash("End-of-day report saved.", "success")
    return redirect(url_for("daily.index") + "#report")


@bp.route("/history")
@login_required
def history():
    """Full history of this member's own check-ins and reports."""
    checkin_query = (
        DailyCheckin.query.filter_by(user_id=current_user.id)
        .order_by(DailyCheckin.workday_date.desc())
    )
    report_query = (
        DailyReport.query.filter_by(user_id=current_user.id)
        .order_by(DailyReport.workday_date.desc())
    )
    return render_template(
        "daily/history.html",
        page_title="My daily record",
        checkins=paginate(checkin_query, per_page=20),
        reports=paginate(report_query, per_page=20),
    )


# ---------------------------------------------------------------------------
# Workday calendar and approved absences (CEO)
# ---------------------------------------------------------------------------

@bp.route("/workdays")
@login_required
def workdays():
    """Company calendar: holidays, non-working days and approved absences."""
    if not is_ceo(current_user):
        abort(403)

    from app.models.focus import Workday, WorkdayException

    start = parse_date(request.args.get("start"), field="Start date")
    end = parse_date(request.args.get("end"), field="End date")

    from app.utils.dates import company_today

    today = company_today()
    if start is None:
        start = today.replace(day=1)
    if end is None:
        from datetime import timedelta

        end = start + timedelta(days=60)

    holidays = (
        Workday.query.filter(Workday.date >= start, Workday.date <= end)
        .order_by(Workday.date.asc())
        .all()
    )
    exceptions = (
        WorkdayException.query.filter(
            WorkdayException.date >= start, WorkdayException.date <= end
        )
        .order_by(WorkdayException.date.asc())
        .all()
    )

    members = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()
    days = workday_service.working_days_between(start, end)

    return render_template(
        "daily/workdays.html",
        page_title="Workday calendar",
        start=start,
        end=end,
        holidays=holidays,
        exceptions=exceptions,
        members=members,
        working_day_count=len(days),
        workweek=sorted(workday_service.working_week()),
    )


@bp.route("/workdays", methods=["POST"])
@login_required
def set_workday():
    if not is_ceo(current_user):
        abort(403)

    try:
        day = parse_date(request.form.get("date"), field="Date")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("daily.workdays"))

    if day is None:
        flash("Choose a date.", "error")
        return redirect(url_for("daily.workdays"))

    is_working = request.form.get("is_working_day") == "1"
    try:
        workday_service.set_company_holiday(
            day,
            is_working=is_working,
            note=request.form.get("note"),
            actor=current_user,
        )
    except Exception as exc:  # pragma: no cover - surfaced rather than hidden
        db.session.rollback()
        flash(f"That could not be saved: {exc}", "error")
    else:
        flash(
            f"{day.isoformat()} marked as "
            f"{'a working day' if is_working else 'a non-working day'}.",
            "success",
        )
    return redirect(url_for("daily.workdays"))


@bp.route("/absences", methods=["POST"])
@login_required
def add_absence():
    """Record an approved absence. Approved leave never costs XP."""
    if not is_ceo(current_user):
        abort(403)

    from app.models.focus import WorkdayExceptionKind

    try:
        day = parse_date(request.form.get("date"), field="Date")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("daily.workdays"))

    member_id = request.form.get("user_id", type=int)
    member = db.session.get(User, member_id) if member_id else None
    if member is None:
        flash("Choose the member the absence applies to.", "error")
        return redirect(url_for("daily.workdays"))
    if day is None:
        flash("Choose a date.", "error")
        return redirect(url_for("daily.workdays"))

    kind = request.form.get("kind", WorkdayExceptionKind.ABSENCE.value)
    if kind not in WorkdayExceptionKind.values():
        kind = WorkdayExceptionKind.ABSENCE.value

    workday_service.add_absence(
        member,
        day,
        reason=request.form.get("reason"),
        kind=kind,
        approved_by=current_user,
    )
    flash(f"Absence recorded for {member.display_name} on {day.isoformat()}.", "success")
    return redirect(url_for("daily.workdays"))


@bp.route("/absences/<int:exception_id>/remove", methods=["POST"])
@login_required
def remove_absence(exception_id: int):
    if not is_ceo(current_user):
        abort(403)

    from app.models.focus import WorkdayException
    from app.services.audit_service import record_audit

    row = db.session.get(WorkdayException, exception_id)
    if row is None:
        abort(404)

    record_audit(
        "workday.absence_removed",
        entity_type="workday_exception",
        entity_id=row.id,
        summary=f"Absence removed for {row.date.isoformat()}",
        before={"date": row.date.isoformat(), "kind": row.kind, "user_id": row.user_id},
        user_id=current_user.id,
    )
    db.session.delete(row)
    db.session.commit()
    flash("Absence record removed.", "info")
    return redirect(url_for("daily.workdays"))


@bp.route("/team")
@login_required
def team_records():
    """CEO view of everyone's daily compliance."""
    if not is_ceo(current_user):
        abort(403)

    days = request.args.get("days", "14")
    try:
        days_int = max(1, min(int(days), 180))
    except (TypeError, ValueError):
        days_int = 14

    start, end = report_service.parse_range(None, None, default_days=days_int)
    members = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()

    return render_template(
        "daily/team.html",
        page_title="Daily records",
        start=start,
        end=end,
        days=days_int,
        rows=report_service.compliance_by_member(start=start, end=end, members=members),
        today=company_today(),
    )
