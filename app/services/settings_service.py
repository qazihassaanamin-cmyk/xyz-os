"""Settings: database-backed configuration with seeded defaults.

Defaults are complete, so the application is fully configured on first launch
without anyone filling in a form. The CEO can adjust values later.
"""

from __future__ import annotations

import logging

from app.extensions import db
from app.models.xp import Setting

log = logging.getLogger(__name__)

DEFAULTS: list[dict] = [
    {
        "key": "company_name",
        "value": "Tavryc",
        "value_type": "str",
        "label": "Company name",
        "description": "Shown in the header, page titles and internal documents.",
        "category": "company",
    },
    {
        "key": "company_tz",
        "value": "Asia/Karachi",
        "value_type": "str",
        "label": "Company time zone",
        "description": "Determines the working day boundary for focus sessions and daily records.",
        "category": "company",
    },
    {
        "key": "workweek_days",
        "value": "1,2,3,4,5",
        "value_type": "str",
        "label": "Working days",
        "description": "ISO weekday numbers, Monday is 1. Weekends are excluded by default.",
        "category": "company",
    },
    {
        "key": "gamification_enabled",
        "value": "true",
        "value_type": "bool",
        "label": "Gamification enabled",
        "description": "Turns XP awards, achievements and challenges on or off.",
        "category": "gamification",
    },
    {
        "key": "leaderboard_enabled",
        "value": "true",
        "value_type": "bool",
        "label": "Show contribution leaderboard",
        "description": "Hides the ranking list while keeping each member's own progress visible.",
        "category": "gamification",
    },
    {
        "key": "leaderboard_show_totals",
        "value": "true",
        "value_type": "bool",
        "label": "Show XP totals publicly",
        "description": "When off, members see levels and progress but not each other's totals.",
        "category": "gamification",
    },
    {
        "key": "challenges_enabled",
        "value": "true",
        "value_type": "bool",
        "label": "Weekly challenges enabled",
        "description": "Rotates a small set of verified weekly challenges.",
        "category": "gamification",
    },
    {
        "key": "xp_deductions_enabled",
        "value": "true",
        "value_type": "bool",
        "label": "Allow manual XP deductions",
        "description": "Required for corrections. Every deduction records a reason.",
        "category": "gamification",
    },
    {
        "key": "anonymous_suggestions_enabled",
        "value": "false",
        "value_type": "bool",
        "label": "Allow anonymous suggestions",
        "description": (
            "Off by default. On a four-person team, writing style can still "
            "identify an author, so this is a limited protection, not anonymity."
        ),
        "category": "suggestions",
    },
    {
        "key": "suggestion_votes_enabled",
        "value": "true",
        "value_type": "bool",
        "label": "Allow voting on suggestions",
        "description": "Popularity is shown for information only; it never decides an outcome.",
        "category": "suggestions",
    },
    {
        "key": "upload_max_mb",
        "value": "5",
        "value_type": "int",
        "label": "Maximum upload size (MB)",
        "description": "Keep this small on the free hosting tier.",
        "category": "storage",
    },
    {
        "key": "upload_max_files",
        "value": "3",
        "value_type": "int",
        "label": "Maximum files per submission",
        "description": "Enforced server-side, not in the browser.",
        "category": "storage",
    },
    {
        "key": "upload_per_member_mb",
        "value": "60",
        "value_type": "int",
        "label": "Upload allowance per member (MB)",
        "description": "Protects the shared storage budget from one large uploader.",
        "category": "storage",
    },
    {
        "key": "upload_retention_days",
        "value": "30",
        "value_type": "int",
        "label": "Deleted file retention (days)",
        "description": "Removed files are kept this long before permanent purging.",
        "category": "storage",
    },
    {
        "key": "report_edit_cutoff_hour",
        "value": "23",
        "value_type": "int",
        "label": "Daily record cutoff hour",
        "description": "Company-local hour after which today's check-in and report are locked.",
        "category": "daily",
    },
    {
        "key": "focus_minutes",
        "value": "20",
        "value_type": "int",
        "label": "Minimum focus session (minutes)",
        "description": "A session only counts once it reaches this length.",
        "category": "focus",
    },
    {
        "key": "focus_pause_limit_minutes",
        "value": "30",
        "value_type": "int",
        "label": "Pause limit (minutes)",
        "description": "A session paused longer than this is recorded as interrupted.",
        "category": "focus",
    },
    {
        "key": "lounge_poll_seconds",
        "value": "15",
        "value_type": "int",
        "label": "Lounge refresh interval (seconds)",
        "description": "Short polling keeps this compatible with shared hosting.",
        "category": "lounge",
    },
    {
        "key": "lounge_rate_limit",
        "value": "20",
        "value_type": "int",
        "label": "Lounge messages per minute",
        "description": "Per member. Enforced server-side.",
        "category": "lounge",
    },
    {
        "key": "notification_email_enabled",
        "value": "false",
        "value_type": "bool",
        "label": "Send notification emails",
        "description": "Requires SMTP to be configured. In-app notifications always work.",
        "category": "notifications",
    },
]

CATEGORY_LABELS = {
    "company": "Company",
    "gamification": "Contribution and rewards",
    "suggestions": "Suggestions",
    "storage": "Files and storage",
    "daily": "Daily records",
    "focus": "Focus timer",
    "lounge": "The Lounge",
    "notifications": "Notifications",
}


def ensure_defaults() -> int:
    """Insert any missing setting. Existing values are never overwritten."""
    created = 0
    for spec in DEFAULTS:
        if Setting.query.filter_by(key=spec["key"]).first() is not None:
            continue
        db.session.add(Setting(**spec))
        created += 1
    if created:
        db.session.commit()
    return created


def get(key: str, default=None):
    return Setting.get(key, default)


def get_int(key: str, default: int) -> int:
    value = Setting.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    value = Setting.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def set_value(key: str, value, *, actor=None) -> Setting:
    from app.services.audit_service import record_audit

    row = Setting.query.filter_by(key=key).first()
    if row is None:
        raise KeyError(f"Unknown setting: {key}")

    before = row.value
    if row.value_type == "bool":
        row.value = "true" if value else "false"
    else:
        row.value = None if value is None else str(value)
    db.session.commit()

    record_audit(
        "setting.updated",
        entity_type="setting",
        entity_id=row.id,
        summary=f"Setting “{row.label or key}” updated",
        before={"value": before},
        after={"value": row.value},
        user_id=getattr(actor, "id", None),
    )
    return row


def grouped() -> dict[str, list[Setting]]:
    """Settings grouped for the CEO's settings page."""
    rows = Setting.query.order_by(Setting.category.asc(), Setting.id.asc()).all()
    groups: dict[str, list[Setting]] = {}
    for row in rows:
        groups.setdefault(row.category, []).append(row)
    return groups


def apply_runtime_overrides(app) -> None:
    """Push stored settings into the Flask config at startup.

    Only non-secret, operational values are copied; secrets stay in the
    environment where they belong.
    """
    try:
        with app.app_context():
            from sqlalchemy import inspect as _sa_inspect

            # A brand-new deployment runs this before ``flask db upgrade``. The
            # table will not exist yet, and asking for a setting would raise on
            # every boot until the schema is applied. Skip cleanly instead.
            if "settings" not in _sa_inspect(db.engine).get_table_names():
                log.info("Settings table not present yet; keeping file configuration.")
                return

            app.config["COMPANY_TZ"] = get("company_tz", app.config.get("COMPANY_TZ"))
            app.config["COMPANY_NAME"] = get("company_name", app.config.get("COMPANY_NAME"))
            app.config["MAX_UPLOAD_MB"] = get_int("upload_max_mb", app.config.get("MAX_UPLOAD_MB", 5))
            app.config["MAX_FILES_PER_SUBMISSION"] = get_int(
                "upload_max_files", app.config.get("MAX_FILES_PER_SUBMISSION", 3)
            )
            app.config["PER_USER_UPLOAD_LIMIT_MB"] = get_int("upload_per_member_mb", 60)
            app.config["REPORT_EDIT_CUTOFF_HOUR"] = get_int(
                "report_edit_cutoff_hour", app.config.get("REPORT_EDIT_CUTOFF_HOUR", 23)
            )
            app.config["FOCUS_MIN_SECONDS"] = get_int("focus_minutes", 20) * 60
            app.config["FOCUS_PAUSE_IDLE_LIMIT_SECONDS"] = (
                get_int("focus_pause_limit_minutes", 30) * 60
            )
    except Exception:  # pragma: no cover - a broken database must not stop boot
        log.warning("Stored settings could not be applied; using file configuration.", exc_info=True)
