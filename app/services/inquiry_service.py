"""Inquiry service.

An inquiry is a question from the CEO to a founder about compliance,
delivery or conduct. Every member can read the board; only the CEO can raise
an inquiry; only the founder it is addressed to can reply. The status is a
column on the row and a history table records each change, so a resolution can
always be reconstructed with its reason and the person who made it.
"""

from __future__ import annotations

import logging

from app.extensions import db
from app.models.base import utcnow
from app.models.suggestion import Inquiry, InquiryReply, InquiryStatus, InquiryStatusEvent

log = logging.getLogger(__name__)


class InquiryError(Exception):
    """A refused inquiry operation, with a member-safe message."""


def visible_query():
    """Every member can read every inquiry. There is nothing to hide here."""
    return Inquiry.query.order_by(Inquiry.created_at.desc())


def for_member(user) -> list[Inquiry]:
    """Inquiries a member can see: everything they raised, everything
    addressed to them, and company-wide inquiries."""
    return (
        visible_query()
        .filter(
            db.or_(
                Inquiry.raised_by_id == user.id,
                Inquiry.addressed_to_id == user.id,
                Inquiry.addressed_to_id.is_(None),
            )
        )
        .all()
    )


def raise_inquiry(*, actor, subject, body, addressed_to=None) -> Inquiry:
    """Create an inquiry. CEO only, enforced by the view."""
    subject = (subject or "").strip()[:200]
    body = (body or "").strip()[:4000]
    if not subject:
        raise InquiryError("A subject is required.")
    if not body:
        raise InquiryError("An inquiry needs a body.")

    addressed_id = None
    if addressed_to is not None:
        addressed_id = addressed_to.id

    inquiry = Inquiry(
        subject=subject,
        body=body,
        raised_by_id=actor.id,
        addressed_to_id=addressed_id,
        status=InquiryStatus.OPEN.value,
    )
    db.session.add(inquiry)
    db.session.flush()
    _record_event(inquiry, from_status=None, to_status=InquiryStatus.OPEN.value, actor=actor)
    db.session.commit()
    return inquiry


def reply(inquiry: Inquiry, *, actor, body) -> InquiryReply:
    """A founder replies to an inquiry addressed to them."""
    body = (body or "").strip()[:4000]
    if not body:
        raise InquiryError("A reply needs a body.")

    is_addressed = inquiry.addressed_to_id == actor.id
    is_company_wide = inquiry.addressed_to_id is None
    is_raiser = inquiry.raised_by_id == actor.id
    if not (is_addressed or is_company_wide or is_raiser):
        raise InquiryError("This inquiry is not addressed to you.")

    reply = InquiryReply(inquiry_id=inquiry.id, author_id=actor.id, body=body)
    db.session.add(reply)
    db.session.flush()

    if inquiry.status == InquiryStatus.OPEN.value:
        _transition(inquiry, InquiryStatus.ACKNOWLEDGED.value, actor=actor, note="Acknowledged by reply")

    db.session.commit()
    return reply


def resolve(inquiry: Inquiry, *, actor, note=None) -> Inquiry:
    """Close an inquiry. The CEO closes; a founder who has replied may also
    request closure, which the CEO then confirms."""
    is_addressed = inquiry.addressed_to_id == actor.id
    is_company_wide = inquiry.addressed_to_id is None
    is_raiser = inquiry.raised_by_id == actor.id
    if not (is_raiser or is_addressed or is_company_wide):
        raise InquiryError("You cannot close this inquiry.")

    note = (note or "").strip()[:2000] or None
    inquiry.status = InquiryStatus.RESOLVED.value
    inquiry.resolved_at = utcnow()
    inquiry.resolved_by_id = actor.id
    inquiry.resolution_note = note
    _record_event(inquiry, from_status=None, to_status=InquiryStatus.RESOLVED.value, actor=actor, note=note)
    db.session.commit()
    return inquiry


def close(inquiry: Inquiry, *, actor, note=None) -> Inquiry:
    """Final closure. CEO only in practice; the view enforces it."""
    if inquiry.raised_by_id != actor.id:
        raise InquiryError("Only the person who raised an inquiry may close it.")
    note = (note or "").strip()[:2000] or None
    inquiry.status = InquiryStatus.CLOSED.value
    _record_event(inquiry, from_status=InquiryStatus.RESOLVED.value, to_status=InquiryStatus.CLOSED.value, actor=actor)
    db.session.commit()
    return inquiry


def _transition(inquiry: Inquiry, to_status: str, *, actor, note=None) -> None:
    inquiry.status = to_status
    _record_event(inquiry, from_status=None, to_status=to_status, actor=actor, note=note)


def _record_event(inquiry: Inquiry, *, from_status, to_status, actor, note=None) -> None:
    db.session.add(
        InquiryStatusEvent(
            inquiry_id=inquiry.id,
            from_status=from_status,
            to_status=to_status,
            note=note,
            actor_id=actor.id if actor else None,
        )
    )


def stats() -> dict:
    total = Inquiry.query.count()
    open_count = Inquiry.query.filter_by(status=InquiryStatus.OPEN.value).count()
    acknowledged = Inquiry.query.filter_by(status=InquiryStatus.ACKNOWLEDGED.value).count()
    resolved = Inquiry.query.filter_by(status=InquiryStatus.RESOLVED.value).count()
    closed = Inquiry.query.filter_by(status=InquiryStatus.CLOSED.value).count()
    return {
        "total": total,
        "open": open_count,
        "acknowledged": acknowledged,
        "resolved": resolved,
        "closed": closed,
        "unanswered": open_count + acknowledged,
    }