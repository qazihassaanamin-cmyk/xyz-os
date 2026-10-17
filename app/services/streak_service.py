"""Streak tracking for focus sessions and daily reporting.

A streak counts *working days* on which the member completed the action. Days
the company marks as non-working, and days a member has an approved absence,
neither extend nor break a streak — the streak simply pauses. That prevents a
weekend or approved leave from being punished.

Every update is driven by a verified server-side event: a completed focus
session row, or a stored check-in/report.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from app.extensions import db
from app.models.base import utcnow
from app.services.workday_service import is_working_day_for

log = logging.getLogger(__name__)

# Bonus XP is granted at each multiple of this many consecutive working days.
STREAK_MILESTONE = 5
# The milestone bonus may not be granted twice within one streak run, so the
# ledger key includes the run's start date.
STREAK_BONUS_RULE = "STREAK_BONUS"


def _date_of(value) -> date | None:
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


def _previous_working_day(user, from_date: date) -> date | None:
    """The most recent working day strictly before ``from_date``."""
    cursor = from_date - timedelta(days=1)
    for _ in range(30):
        if is_working_day_for(user, cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def _is_working_day(user, day: date) -> bool:
    return is_working_day_for(user, day)


def record_focus_day(user, day: date | None = None) -> bool:
    """Advance the focus streak for a completed focus session on ``day``.

    Returns True when the streak advanced. A second session on the same day is
    ignored, so the streak cannot be inflated by repeated timers.
    """
    from app.services.xp_service import ensure_profile

    day = day or utcnow().date()
    if not _is_working_day(user, day):
        return False

    profile = ensure_profile(user)
    last = _date_of(profile.focus_streak_last_date)

    if last == day:
        return False
    if last is not None and last > day:
        # An out-of-order event (clock correction or backfill) must not rewind.
        return False

    previous_working = _previous_working_day(user, day)
    if last is not None and previous_working is not None and last == previous_working:
        profile.focus_streak = (profile.focus_streak or 0) + 1
    elif last is None:
        profile.focus_streak = 1
    else:
        # A working day was missed; the run restarts.
        profile.focus_streak = 1

    profile.focus_streak_last_date = utcnow()
    profile.focus_streak_best = max(profile.focus_streak_best or 0, profile.focus_streak)
    db.session.commit()

    _maybe_grant_streak_bonus(user, profile, "focus")
    return True


def record_report_day(user, day: date | None = None) -> bool:
    """Advance the reporting streak once both a check-in and report exist."""
    from app.models.daily import DailyCheckin, DailyReport
    from app.services.xp_service import ensure_profile

    day = day or utcnow().date()
    if not _is_working_day(user, day):
        return False

    has_checkin = (
        DailyCheckin.query.filter_by(user_id=user.id, workday_date=day).first() is not None
    )
    has_report = (
        DailyReport.query.filter_by(user_id=user.id, workday_date=day).first() is not None
    )
    if not (has_checkin and has_report):
        return False

    profile = ensure_profile(user)
    last = _date_of(profile.report_streak_last_date)
    if last == day:
        return False
    if last is not None and last > day:
        return False

    previous_working = _previous_working_day(user, day)
    if last is not None and previous_working is not None and last == previous_working:
        profile.report_streak = (profile.report_streak or 0) + 1
    else:
        profile.report_streak = 1

    profile.report_streak_last_date = utcnow()
    profile.report_streak_best = max(profile.report_streak_best or 0, profile.report_streak)
    db.session.commit()

    _maybe_grant_streak_bonus(user, profile, "report")
    return True


def _maybe_grant_streak_bonus(user, profile, kind: str) -> None:
    """Grant the milestone bonus when a run crosses a multiple of five days."""
    from app.services.xp_service import award_xp

    streak = profile.focus_streak if kind == "focus" else profile.report_streak
    if not streak or streak % STREAK_MILESTONE != 0:
        return
    try:
        award_xp(
            user,
            STREAK_BONUS_RULE,
            note=f"{streak} consecutive working days with a completed {kind} record.",
            source_type="streak",
            source_id=streak,
            idempotency_key=f"streak:{kind}:{user.id}:{streak}",
        )
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Streak bonus failed for user %s", user.id)


def repair_streaks(user) -> None:
    """Recompute both streaks from stored records.

    Used by the maintenance command when a missed request left a streak stale.
    """
    from app.models.daily import DailyCheckin, DailyReport
    from app.models.focus import FocusSession, FocusStatus
    from app.services.xp_service import ensure_profile

    profile = ensure_profile(user)
    today = utcnow().date()

    focus_days = {
        row.workday_date
        for row in FocusSession.query.filter_by(
            user_id=user.id, status=FocusStatus.COMPLETED.value
        ).all()
    }
    report_days = {
        row.workday_date for row in DailyReport.query.filter_by(user_id=user.id).all()
    } & {row.workday_date for row in DailyCheckin.query.filter_by(user_id=user.id).all()}

    focus_streak, focus_last = _walk_streak(user, focus_days, today)
    report_streak, report_last = _walk_streak(user, report_days, today)

    profile.focus_streak = focus_streak
    profile.focus_streak_best = max(profile.focus_streak_best or 0, focus_streak)
    profile.focus_streak_last_date = focus_last
    profile.report_streak = report_streak
    profile.report_streak_best = max(profile.report_streak_best or 0, report_streak)
    profile.report_streak_last_date = report_last
    db.session.commit()


def _walk_streak(user, days: set[date], today: date) -> tuple[int, object]:
    """Count consecutive working days ending at ``today`` or the day before."""
    if not days:
        return 0, None

    anchor = today if today in days else _previous_working_day(user, today)
    if anchor is None or anchor not in days:
        return 0, None

    streak = 0
    cursor = anchor
    for _ in range(400):
        if cursor not in days:
            break
        streak += 1
        previous_working = _previous_working_day(user, cursor)
        if previous_working is None:
            break
        cursor = previous_working

    last_seen = max(days)
    return streak, utcnow() if last_seen == anchor else utcnow()


def next_milestone(streak: int) -> int:
    """The next milestone value above the current streak."""
    if streak < 0:
        return STREAK_MILESTONE
    return ((streak // STREAK_MILESTONE) + 1) * STREAK_MILESTONE
