"""Redirect and URL safety helpers."""

from __future__ import annotations

from urllib.parse import urlparse

from flask import redirect, request, url_for


def is_safe_redirect_target(target: str | None) -> bool:
    """True only for a same-site absolute path.

    Rejects absolute URLs, scheme-relative URLs, backslash tricks and control
    characters that some browsers normalise into a scheme separator.
    """
    if not target or not isinstance(target, str):
        return False
    if len(target) > 2000:
        return False
    if any(ord(char) < 0x20 for char in target):
        return False
    if "\\" in target:
        return False
    if not target.startswith("/"):
        return False
    if target.startswith("//"):
        return False
    # "/\t/evil.com" and similar normalisations.
    if target.startswith("/\\") or target.startswith("/\t"):
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return True


def safe_redirect(target: str | None, *, fallback_endpoint: str = "dashboard.index", **values):
    """Redirect to ``target`` only when it is a safe internal path."""
    if is_safe_redirect_target(target):
        return redirect(target)
    return redirect(url_for(fallback_endpoint, **values))


def current_path() -> str:
    path = request.path or "/"
    if request.query_string:
        return f"{path}?{request.query_string.decode('utf-8', 'ignore')}"
    return path
