"""Shared date and time helpers.

The company runs on one timezone, configured in the settings table and seeded
from ``COMPANY_TZ``. All storage is UTC; conversion happens at the edges.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app

from app.models.base import utcnow


def company_tz() -> ZoneInfo:
    """The company timezone, resolved from settings then config."""
    name = None
    try:
        from app.models.xp import Setting

        name = Setting.get("company_tz")
    except Exception:  # pragma: no cover - during migration or first boot
        name = None
    name = name or current_app.config.get("COMPANY_TZ", "Asia/Karachi")
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_company_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(company_tz())


def company_today() -> date:
    """Today's date in the company timezone."""
    return to_company_time(utcnow()).date()


def company_now() -> datetime:
    return to_company_time(utcnow())


def workday_for(moment: datetime | None = None) -> date:
    """The company-local workday a UTC moment belongs to."""
    moment = moment or utcnow()
    return to_company_time(moment).date()


def day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """UTC start and end instants for a company-local calendar day."""
    tz = company_tz()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def humanise_delta(value: datetime | None) -> str:
    """Short relative time such as "4 hours ago" or "in 2 days"."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = utcnow() - value
    seconds = int(delta.total_seconds())
    future = seconds < 0
    seconds = abs(seconds)

    if seconds < 60:
        text = "just now" if not future else "in a moment"
        return text
    units = (
        (31536000, "year"),
        (2592000, "month"),
        (604800, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
    )
    for size, label in units:
        if seconds >= size:
            count = seconds // size
            plural = "s" if count != 1 else ""
            return f"in {count} {label}{plural}" if future else f"{count} {label}{plural} ago"
    return "just now"


def humanise_seconds(total_seconds: int | None) -> str:
    """Duration such as "1h 24m" or "18m 05s"."""
    if total_seconds is None:
        return "—"
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_clock(total_seconds: int | None) -> str:
    """MM:SS or H:MM:SS for the focus timer display."""
    if total_seconds is None:
        return "00:00"
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def deadline_state(due: date | None, *, completed: bool = False) -> str:
    if completed:
        return "completed"
    if due is None:
        return "none"
    delta = (due - company_today()).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta <= 2:
        return "soon"
    return "later"


def deadline_label(due: date | None) -> str:
    if due is None:
        return "No deadline"
    delta = (due - company_today()).days
    if delta == 0:
        return "Due today"
    if delta == 1:
        return "Due tomorrow"
    if delta == -1:
        return "1 day overdue"
    if delta < 0:
        return f"{abs(delta)} days overdue"
    return f"Due in {delta} days"


def parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating the SQLite 'Z' suffix and
    space-separated forms. Returns None when the string cannot be parsed.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
