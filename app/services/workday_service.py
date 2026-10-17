"""Workday calendar: which dates count as working days, and for whom.

A date is a working day when it is not a company holiday and the member has no
approved absence. Weekends are excluded by default but the CEO can change the
working week from the settings table.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from app.extensions import db
from app.models.base import utcnow
from app.models.focus import Workday, WorkdayException, WorkdayExceptionKind
from app.models.xp import Setting

log = logging.getLogger(__name__)

SETTING_WORKWEEK = "workweek_days"


def working_week() -> set[int]:
    """ISO weekday numbers treated as working days. Monday is 1."""
    raw = Setting.get(SETTING_WORKWEEK, "1,2,3,4,5")
    if isinstance(raw, str):
        days: set[int] = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and 1 <= int(chunk) <= 7:
                days.add(int(chunk))
        return days or {1, 2, 3, 4, 5}
    return {1, 2, 3, 4, 5}


def _is_weekend_working_day(day: date) -> bool:
    return day.isoweekday() in working_week()


def company_holiday(day: date) -> Workday | None:
    row = Workday.query.filter_by(date=day).first()
    if row is None:
        return None
    return row


def is_working_day(day: date) -> bool:
    """Company-level answer, ignoring individual absences."""
    row = company_holiday(day)
    if row is not None:
        return bool(row.is_working_day)
    return _is_weekend_working_day(day)


def is_working_day_for(user, day: date) -> bool:
    """Member-level answer: a working day unless they are on approved leave."""
    if user is not None and getattr(user, "id", None) is not None:
        if has_absence(user, day):
            return False
    return is_working_day(day)


def has_absence(user, day: date) -> bool:
    if user is None or getattr(user, "id", None) is None:
        return False
    return (
        WorkdayException.query.filter_by(user_id=user.id, date=day).first() is not None
        or WorkdayException.query.filter_by(user_id=None, date=day).first() is not None
    )


def working_days_between(start: date, end: date, user=None) -> list[date]:
    """Inclusive list of working days between two dates."""
    if end < start:
        return []
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if is_working_day_for(user, cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def current_streak_window(user, day: date | None = None) -> date | None:
    """The most recent working day at or before ``day`` for this member."""
    day = day or utcnow().date()
    cursor = day
    for _ in range(30):
        if is_working_day_for(user, cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def previous_working_day(user, day: date) -> date | None:
    """The most recent working day strictly before ``day``."""
    cursor = day - timedelta(days=1)
    for _ in range(30):
        if is_working_day_for(user, cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def week_bounds(day: date | None = None) -> tuple[date, date]:
    """Monday-to-Sunday bounds containing ``day``."""
    day = day or utcnow().date()
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date | None = None) -> tuple[date, date]:
    day = day or utcnow().date()
    start = day.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1) - timedelta(days=1)
    return start, end


def set_company_holiday(day: date, *, is_working: bool, note: str | None = None, actor=None) -> Workday:
    from app.services.audit_service import record_audit

    row = Workday.query.filter_by(date=day).first()
    if row is None:
        row = Workday(date=day, is_working_day=is_working, note=note)
        db.session.add(row)
    else:
        row.is_working_day = is_working
        row.note = note
    db.session.commit()

    record_audit(
        "workday.updated",
        entity_type="workday",
        entity_id=row.id,
        summary=f"{day.isoformat()} marked as {'a working day' if is_working else 'a non-working day'}",
        after={"date": day.isoformat(), "is_working_day": is_working},
        user_id=getattr(actor, "id", None),
    )
    return row


def add_absence(
    user,
    day: date,
    *,
    reason: str | None = None,
    kind: str = WorkdayExceptionKind.ABSENCE.value,
    approved_by=None,
) -> WorkdayException:
    """Record an approved absence. Approved leave never costs XP."""
    from app.services.audit_service import record_audit

    existing = WorkdayException.query.filter_by(
        user_id=getattr(user, "id", None), date=day, kind=kind
    ).first()
    if existing is not None:
        existing.reason = reason
        row = existing
    else:
        row = WorkdayException(
            user_id=getattr(user, "id", None),
            date=day,
            kind=kind,
            reason=reason,
            approved_by_id=getattr(approved_by, "id", None),
            approved_at=utcnow() if approved_by is not None else None,
        )
        db.session.add(row)
    db.session.commit()

    record_audit(
        "workday.absence_recorded",
        entity_type="workday_exception",
        entity_id=row.id,
        summary=f"Absence recorded for user #{getattr(user, 'id', None)} on {day.isoformat()}",
        after={"date": day.isoformat(), "kind": kind},
        user_id=getattr(approved_by, "id", None),
    )
    return row
