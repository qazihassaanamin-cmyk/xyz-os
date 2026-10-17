"""Daily check-ins and end-of-day reports.

One check-in and one report per member per company workday, enforced by unique
constraints. Both are editable until the day's cutoff; every edit keeps a
revision snapshot and writes an audit row, so the record of what was originally
claimed is never lost.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import utcnow
from app.models.daily import DailyCheckin, DailyReport, DailyReportRevision
from app.services.audit_service import record_audit
from app.utils.dates import company_today, company_tz, workday_for
from app.utils.validators import optional_text, require_text

log = logging.getLogger(__name__)


class DailyError(Exception):
    """A refused daily-record operation, with a member-safe message."""


def _cutoff_hour() -> int:
    from flask import current_app

    return int(current_app.config.get("REPORT_EDIT_CUTOFF_HOUR", 23))


def is_editable(workday_date, user=None) -> bool:
    """True while today's records may still be written."""
    today = company_today()
    if workday_date != today:
        return False
    now_local = datetime.now(company_tz())
    cutoff = time(hour=_cutoff_hour(), minute=59, tzinfo=now_local.tzinfo)
    return now_local.time() <= cutoff


def _assert_editable(workday_date) -> None:
    if workday_date != company_today():
        raise DailyError(
            "That day has closed. Only today's record can be written or edited."
        )
    if not is_editable(workday_date):
        raise DailyError(
            f"The editing window for today closed at {_cutoff_hour():02d}:59 company time."
        )


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------

def get_checkin(user, workday_date=None) -> DailyCheckin | None:
    workday_date = workday_date or company_today()
    return DailyCheckin.query.filter_by(user_id=user.id, workday_date=workday_date).first()


def save_checkin(
    user,
    *,
    priorities: str,
    intended_result: str | None = None,
    support_needed: str | None = None,
    blockers: str | None = None,
    workday_date=None,
) -> DailyCheckin:
    """Create or update today's check-in."""
    workday_date = workday_date or workday_for()
    _assert_editable(workday_date)

    priorities = require_text(
        priorities, field="Today's priorities", min_length=5, max_length=4000
    )
    intended_result = optional_text(intended_result, max_length=2000)
    support_needed = optional_text(support_needed, max_length=2000)
    blockers = optional_text(blockers, max_length=2000)

    existing = get_checkin(user, workday_date)
    created = existing is None

    if created:
        row = DailyCheckin(
            user_id=user.id,
            workday_date=workday_date,
            priorities=priorities,
            intended_result=intended_result,
            support_needed=support_needed,
            blockers=blockers,
        )
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            row = get_checkin(user, workday_date)
            row.priorities = priorities
            row.intended_result = intended_result
            row.support_needed = support_needed
            row.blockers = blockers
            db.session.commit()
            created = False
    else:
        row = existing
        row.priorities = priorities
        row.intended_result = intended_result
        row.support_needed = support_needed
        row.blockers = blockers
        db.session.commit()

    record_audit(
        "daily.checkin_saved" if created else "daily.checkin_updated",
        entity_type="daily_checkin",
        entity_id=row.id,
        summary=(
            f"Check-in submitted for {workday_date.isoformat()}"
            if created
            else f"Check-in updated for {workday_date.isoformat()}"
        ),
        user_id=user.id,
    )

    if created:
        _award_checkin_xp(user, row)
        _grant_first_checkin(user)
    _maybe_streak(user, workday_date)
    return row


def _award_checkin_xp(user, checkin: DailyCheckin) -> None:
    from app.models.xp import XpReason
    from app.services import xp_service

    try:
        xp_service.award_xp(
            user,
            XpReason.CHECKIN_COMPLETED.value,
            note=f"Daily check-in submitted for {checkin.workday_date.isoformat()}.",
            source_type="daily_checkin",
            source_id=checkin.id,
            idempotency_key=f"checkin:{user.id}:{checkin.workday_date.isoformat()}",
        )
        checkin.xp_awarded_at = utcnow()
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Check-in XP award failed for user %s", user.id)


def _grant_first_checkin(user) -> None:
    try:
        from app.services.achievement_service import evaluate_all

        evaluate_all(user)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Achievement evaluation failed after check-in")


def _maybe_streak(user, workday_date) -> None:
    from app.services.streak_service import record_report_day

    try:
        record_report_day(user, workday_date)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Reporting streak update failed for user %s", user.id)


# ---------------------------------------------------------------------------
# End-of-day report
# ---------------------------------------------------------------------------

def get_report(user, workday_date=None) -> DailyReport | None:
    workday_date = workday_date or company_today()
    return DailyReport.query.filter_by(user_id=user.id, workday_date=workday_date).first()


def save_report(
    user,
    *,
    accomplished: str,
    submitted_summary: str | None = None,
    unfinished: str | None = None,
    unfinished_reason: str | None = None,
    next_steps: str | None = None,
    workday_date=None,
) -> DailyReport:
    """Create or update today's report, snapshotting the previous version."""
    workday_date = workday_date or workday_for()
    _assert_editable(workday_date)

    accomplished = require_text(
        accomplished, field="What you accomplished", min_length=5, max_length=6000
    )
    submitted_summary = optional_text(submitted_summary, max_length=4000)
    unfinished = optional_text(unfinished, max_length=4000)
    unfinished_reason = optional_text(unfinished_reason, max_length=4000)
    next_steps = optional_text(next_steps, max_length=4000)

    existing = get_report(user, workday_date)
    created = existing is None

    if created:
        row = DailyReport(
            user_id=user.id,
            workday_date=workday_date,
            accomplished=accomplished,
            submitted_summary=submitted_summary,
            unfinished=unfinished,
            unfinished_reason=unfinished_reason,
            next_steps=next_steps,
            revision_count=0,
        )
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            row = get_report(user, workday_date)
            created = False
    else:
        row = existing

    if not created:
        _snapshot(row, user)
        row.accomplished = accomplished
        row.submitted_summary = submitted_summary
        row.unfinished = unfinished
        row.unfinished_reason = unfinished_reason
        row.next_steps = next_steps
        row.revision_count = (row.revision_count or 0) + 1
        db.session.commit()

    record_audit(
        "daily.report_submitted" if created else "daily.report_updated",
        entity_type="daily_report",
        entity_id=row.id,
        summary=(
            f"End-of-day report submitted for {workday_date.isoformat()}"
            if created
            else f"End-of-day report updated for {workday_date.isoformat()} "
            f"(revision {row.revision_count})"
        ),
        user_id=user.id,
    )

    if created:
        _award_report_xp(user, row)
        _grant_first_report(user)
    _maybe_streak(user, workday_date)
    return row


def _snapshot(report: DailyReport, user) -> None:
    revision = DailyReportRevision(
        report_id=report.id,
        snapshot={
            "accomplished": report.accomplished,
            "submitted_summary": report.submitted_summary,
            "unfinished": report.unfinished,
            "unfinished_reason": report.unfinished_reason,
            "next_steps": report.next_steps,
        },
        edited_at=utcnow(),
        edited_by_id=user.id,
    )
    db.session.add(revision)
    db.session.flush()


def _award_report_xp(user, report: DailyReport) -> None:
    from app.models.xp import XpReason
    from app.services import xp_service

    try:
        xp_service.award_xp(
            user,
            XpReason.REPORT_COMPLETED.value,
            note=f"End-of-day report submitted for {report.workday_date.isoformat()}.",
            source_type="daily_report",
            source_id=report.id,
            idempotency_key=f"report:{user.id}:{report.workday_date.isoformat()}",
        )
        report.xp_awarded_at = utcnow()
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Report XP award failed for user %s", user.id)


def _grant_first_report(user) -> None:
    try:
        from app.services.achievement_service import evaluate_all

        evaluate_all(user)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Achievement evaluation failed after report")


# ---------------------------------------------------------------------------
# Queries used by dashboards and the CEO view
# ---------------------------------------------------------------------------

def today_status(user) -> dict:
    """Compact state used by the dashboard and the daily page."""
    today = company_today()
    return {
        "date": today,
        "checkin": get_checkin(user, today),
        "report": get_report(user, today),
        "editable": is_editable(today),
        "cutoff_hour": _cutoff_hour(),
    }


def missing_today(users) -> list:
    """Members who have not yet submitted a check-in today."""
    today = company_today()
    submitted = {
        row.user_id
        for row in DailyCheckin.query.filter_by(workday_date=today).all()
    }
    return [user for user in users if user.id not in submitted and user.is_active]


def compliance_rows(users, *, start, end) -> list[dict]:
    """Per-member check-in and report counts across a date range."""
    rows = []
    for user in users:
        checkins = DailyCheckin.query.filter(
            DailyCheckin.user_id == user.id,
            DailyCheckin.workday_date >= start,
            DailyCheckin.workday_date <= end,
        ).count()
        reports = DailyReport.query.filter(
            DailyReport.user_id == user.id,
            DailyReport.workday_date >= start,
            DailyReport.workday_date <= end,
        ).count()
        rows.append({"user": user, "checkins": checkins, "reports": reports})
    return rows


def history_for(user, *, limit: int = 30) -> dict:
    """Recent check-ins and reports for the member's own history view."""
    checkins = (
        DailyCheckin.query.filter_by(user_id=user.id)
        .order_by(DailyCheckin.workday_date.desc())
        .limit(limit)
        .all()
    )
    reports = (
        DailyReport.query.filter_by(user_id=user.id)
        .order_by(DailyReport.workday_date.desc())
        .limit(limit)
        .all()
    )
    return {"checkins": checkins, "reports": reports}
