"""Shared input validation helpers."""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

MAX_TITLE = 220
MAX_BODY = 20000
MAX_SHORT = 300


class ValidationError(ValueError):
    """Raised with a message safe to show the member."""


def clean_text(value: str | None, *, max_length: int = MAX_BODY, field: str = "Text") -> str:
    """Trim, collapse excessive blank lines and enforce a length limit."""
    if value is None:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{4,}", "\n\n", text)
    if len(text) > max_length:
        raise ValidationError(f"{field} must be {max_length} characters or fewer.")
    return text


def require_text(value: str | None, *, field: str, min_length: int = 1, max_length: int = MAX_BODY) -> str:
    text = clean_text(value, max_length=max_length, field=field)
    if len(text) < min_length:
        raise ValidationError(f"{field} is required.")
    return text


def optional_text(value: str | None, *, max_length: int = MAX_BODY) -> str | None:
    text = clean_text(value, max_length=max_length)
    return text or None


def parse_date(value, *, field: str = "Date") -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValidationError(f"{field} must be a valid date (YYYY-MM-DD).")


def parse_int(value, *, field: str = "Value", minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a whole number.")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{field} must be at most {maximum}.")
    return number


def parse_choice(value, allowed, *, field: str = "Option", required: bool = True) -> str | None:
    text = (value or "").strip().upper()
    if not text:
        if required:
            raise ValidationError(f"Choose a {field.lower()}.")
        return None
    if text not in allowed:
        raise ValidationError(f"“{value}” is not a valid {field.lower()}.")
    return text


def parse_slug(value: str | None, *, fallback: str | None = None) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        if fallback:
            return fallback
        raise ValidationError("Enter a name that contains letters or numbers.")
    return text[:180]


def parse_external_link(value: str | None) -> str | None:
    """Only http and https links are accepted for submission evidence."""
    if not value or not str(value).strip():
        return None
    url = str(value).strip()
    if len(url) > 500:
        raise ValidationError("That link is too long.")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("Enter a full web address starting with http:// or https://.")
    return url


def parse_email(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not EMAIL_RE.match(text):
        raise ValidationError("Enter a valid email address.")
    return text[:255]
