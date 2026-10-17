"""Append-only audit trail.

Every privileged action calls ``record_audit`` with before/after values so the
CEO can reconstruct who changed what and when. The table is never updated or
deleted from application code.
"""

from __future__ import annotations

import json
import logging

from flask import current_app, has_request_context, request, session
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.base import utcnow

log = logging.getLogger(__name__)


def _client_ip() -> str | None:
    if not has_request_context():
        return None
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or None) and request.remote_addr[:64]


def _safe_json(value):
    """Reduce arbitrary values to something JSON-serialisable and bounded."""
    if value is None or isinstance(value, (int, float, bool, str)):
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + "…"
        return value
    if isinstance(value, dict):
        return {str(k)[:64]: _safe_json(v) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v) for v in list(value)[:40]]
    try:
        return _safe_json(str(value))
    except Exception:  # pragma: no cover
        return "<unserialisable>"


def record_audit(
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    summary: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    user_id: int | None = None,
    commit: bool = True,
):
    """Write one audit row. Never raises into the caller's happy path."""
    from app.models.audit import AuditLog

    actor_id = user_id
    if actor_id is None and has_request_context():
        try:
            if current_user and current_user.is_authenticated:
                actor_id = current_user.id
        except Exception:  # pragma: no cover
            actor_id = None

    request_id = None
    if has_request_context():
        request_id = session.get("_request_uid")
        if request_id is None:
            import secrets

            request_id = secrets.token_hex(8)
            session["_request_uid"] = request_id

    row = AuditLog(
        user_id=actor_id,
        action=action[:80],
        entity_type=(entity_type or "")[:60] or None,
        entity_id=entity_id,
        summary=(summary or "")[:500] or None,
        before_json=json.dumps(_safe_json(before), ensure_ascii=False) if before else None,
        after_json=json.dumps(_safe_json(after), ensure_ascii=False) if after else None,
        ip_address=_client_ip(),
        user_agent=(request.headers.get("User-Agent") or "")[:300] if has_request_context() else None,
        request_id=request_id,
    )
    try:
        db.session.add(row)
        if commit:
            db.session.commit()
    except SQLAlchemyError:  # pragma: no cover - audit must not break the request
        db.session.rollback()
        log.exception("Failed to write audit record for action=%s", action)
    return row
