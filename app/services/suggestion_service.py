"""Suggestions: raising, voting, deciding and converting."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import utcnow
from app.models.suggestion import (
    SUGGESTION_STATUS_LABELS,
    Suggestion,
    SuggestionCategory,
    SuggestionStatus,
    SuggestionVote,
)
from app.services.audit_service import record_audit
from app.services.notification_service import suggestion_status_changed
from app.utils.validators import optional_text, require_text

log = logging.getLogger(__name__)

# Allowed status moves. Declared once so the rule is testable.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    SuggestionStatus.OPEN.value: {
        SuggestionStatus.UNDER_REVIEW.value,
        SuggestionStatus.DECLINED.value,
        SuggestionStatus.PLANNED.value,
    },
    SuggestionStatus.UNDER_REVIEW.value: {
        SuggestionStatus.PLANNED.value,
        SuggestionStatus.IMPLEMENTED.value,
        SuggestionStatus.DECLINED.value,
    },
    SuggestionStatus.PLANNED.value: {
        SuggestionStatus.IMPLEMENTED.value,
        SuggestionStatus.DECLINED.value,
        SuggestionStatus.UNDER_REVIEW.value,
    },
    SuggestionStatus.IMPLEMENTED.value: set(),
    SuggestionStatus.DECLINED.value: {SuggestionStatus.UNDER_REVIEW.value},
}


class SuggestionError(Exception):
    """A refused suggestion operation, with a member-safe message."""


def create_suggestion(
    *,
    actor,
    title: str,
    body: str,
    category: str = SuggestionCategory.PROCESS.value,
    project=None,
    is_anonymous: bool = False,
) -> Suggestion:
    from app.services import settings_service

    title = require_text(title, field="Suggestion title", min_length=4, max_length=200)
    body = require_text(body, field="Suggestion", min_length=10, max_length=8000)

    category_value = SuggestionCategory.coerce(category)
    if category_value is None:
        raise SuggestionError("Choose a valid category.")

    if is_anonymous and not settings_service.get_bool("anonymous_suggestions_enabled", False):
        raise SuggestionError(
            "Anonymous suggestions are switched off. Ask the CEO to enable them."
        )

    suggestion = Suggestion(
        title=title,
        body=body,
        category=category_value.value,
        author_id=None if is_anonymous else actor.id,
        is_anonymous=bool(is_anonymous),
        project_id=project.id if project else None,
        status=SuggestionStatus.OPEN.value,
    )
    db.session.add(suggestion)
    db.session.commit()

    if is_anonymous:
        # Deliberately no author in the audit trail. The record still exists so
        # the CEO can see that an anonymous suggestion was made and when.
        record_audit(
            "suggestion.created_anonymous",
            entity_type="suggestion",
            entity_id=suggestion.id,
            summary="Anonymous suggestion submitted",
            after={"category": suggestion.category, "title": suggestion.title},
            user_id=None,
        )
    else:
        record_audit(
            "suggestion.created",
            entity_type="suggestion",
            entity_id=suggestion.id,
            summary=f"Suggestion “{suggestion.title}” submitted",
            after={"category": suggestion.category, "project_id": suggestion.project_id},
            user_id=actor.id,
        )
    return suggestion


def toggle_vote(suggestion: Suggestion, *, actor) -> bool:
    """Idempotent vote toggle. Returns True when a vote was added."""
    from app.services import settings_service

    if not settings_service.get_bool("suggestion_votes_enabled", True):
        raise SuggestionError("Voting is currently switched off.")

    existing = SuggestionVote.query.filter_by(
        suggestion_id=suggestion.id, user_id=actor.id
    ).first()

    if existing is not None:
        db.session.delete(existing)
        suggestion.vote_count = max(0, (suggestion.vote_count or 1) - 1)
        db.session.commit()
        return False

    vote = SuggestionVote(suggestion_id=suggestion.id, user_id=actor.id)
    db.session.add(vote)
    try:
        db.session.commit()
    except IntegrityError:
        # The unique constraint rejected a duplicate; treat as already voted.
        db.session.rollback()
        return False

    suggestion.vote_count = (suggestion.vote_count or 0) + 1
    db.session.commit()
    return True


def has_voted(suggestion: Suggestion, user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return (
        SuggestionVote.query.filter_by(suggestion_id=suggestion.id, user_id=user.id).first()
        is not None
    )


def can_decide(user, suggestion: Suggestion) -> bool:
    """The CEO decides; a project lead may decide suggestions on their project."""
    from app.permissions import is_ceo, is_project_lead

    if is_ceo(user):
        return True
    if suggestion.project is not None and is_project_lead(user, suggestion.project):
        return True
    return False


def change_status(
    suggestion: Suggestion,
    *,
    actor,
    new_status: str,
    note: str | None = None,
) -> Suggestion:
    """Move a suggestion through its lifecycle. Declining requires a reason."""
    if not can_decide(actor, suggestion):
        raise SuggestionError("Only the CEO or the relevant project lead can decide this.")

    target = (new_status or "").strip().upper()
    if target not in SuggestionStatus.values():
        raise SuggestionError("Choose a valid status.")
    if target == suggestion.status:
        raise SuggestionError("The suggestion is already in that status.")

    allowed = STATUS_TRANSITIONS.get(suggestion.status, set())
    if target not in allowed:
        raise SuggestionError(
            f"A suggestion cannot move from {suggestion.status_label} "
            f"to {SUGGESTION_STATUS_LABELS.get(target, target)}."
        )

    note = optional_text(note, max_length=4000)
    if target == SuggestionStatus.DECLINED.value and not note:
        raise SuggestionError(
            "Explain the reason for declining. A clear reason is more useful than a refusal."
        )
    if target == SuggestionStatus.IMPLEMENTED.value and not note:
        raise SuggestionError("Record what was actually implemented.")

    previous = suggestion.status
    suggestion.status = target
    suggestion.decision_note = note
    suggestion.decided_by_id = actor.id
    suggestion.decided_at = utcnow()
    if target == SuggestionStatus.IMPLEMENTED.value:
        suggestion.implemented_at = utcnow()

    db.session.commit()

    record_audit(
        "suggestion.status_changed",
        entity_type="suggestion",
        entity_id=suggestion.id,
        summary=f"Suggestion “{suggestion.title}” moved from {previous} to {target}",
        before={"status": previous},
        after={"status": target, "note": note},
        user_id=actor.id,
    )

    if target == SuggestionStatus.IMPLEMENTED.value:
        _award_implementation(suggestion, actor)

    suggestion_status_changed(suggestion, target, actor=actor)
    return suggestion


def _award_implementation(suggestion: Suggestion, actor) -> None:
    """Reward the author once, and only when the author is known."""
    if suggestion.is_anonymous or suggestion.author is None:
        return
    if suggestion.xp_awarded_at is not None:
        return

    from app.models.xp import XpReason
    from app.services import xp_service

    try:
        xp_service.award_xp(
            suggestion.author,
            XpReason.SUGGESTION_IMPLEMENTED.value,
            note=f"Your suggestion “{suggestion.title}” was implemented.",
            source_type="suggestion",
            source_id=suggestion.id,
            project_id=suggestion.project_id,
            idempotency_key=f"suggestion:{suggestion.id}:implemented",
            awarded_by=actor,
        )
        suggestion.xp_awarded_at = utcnow()
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Suggestion XP award failed for suggestion %s", suggestion.id)


def convert_to_task(suggestion: Suggestion, *, actor, project, title: str | None = None):
    """Turn an approved suggestion into a real task and link back to it."""
    from app.services.task_service import TaskError, create_task

    if not can_decide(actor, suggestion):
        raise SuggestionError("You cannot convert this suggestion.")
    if suggestion.status not in (
        SuggestionStatus.PLANNED.value,
        SuggestionStatus.UNDER_REVIEW.value,
    ):
        raise SuggestionError("Only a planned suggestion can be converted into work.")
    if project is None:
        raise SuggestionError("Choose the project this work belongs to.")

    try:
        task = create_task(
            actor=actor,
            title=title or suggestion.title,
            project=project,
            description=(
                f"Created from a suggestion.\n\n{suggestion.body}\n\n"
                f"Original suggestion: #{suggestion.id}"
            ),
            priority="NORMAL",
        )
    except TaskError as exc:
        raise SuggestionError(str(exc))

    suggestion.converted_to_type = "TASK"
    suggestion.converted_to_id = task.id
    db.session.commit()

    record_audit(
        "suggestion.converted",
        entity_type="suggestion",
        entity_id=suggestion.id,
        summary=f"Suggestion “{suggestion.title}” converted into task #{task.id}",
        after={"task_id": task.id, "project_id": project.id},
        user_id=actor.id,
    )
    return task


def visible_query(user, *, status: str | None = None, category: str | None = None,
                  project_id: int | None = None, mine: bool = False,
                  sort: str = "recent"):
    """Suggestions this member may see, filtered at the query level."""
    query = Suggestion.query
    if status:
        query = query.filter(Suggestion.status == status)
    if category:
        query = query.filter(Suggestion.category == category)
    if project_id:
        query = query.filter(Suggestion.project_id == project_id)
    if mine:
        query = query.filter(Suggestion.author_id == user.id, Suggestion.is_anonymous.is_(False))

    if sort == "popular":
        return query.order_by(Suggestion.vote_count.desc(), Suggestion.created_at.desc())
    if sort == "oldest":
        return query.order_by(Suggestion.created_at.asc())
    return query.order_by(Suggestion.created_at.desc())


def stats() -> dict:
    rows = db.session.query(Suggestion.status, db.func.count(Suggestion.id)).group_by(
        Suggestion.status
    ).all()
    counts = {status: count for status, count in rows}
    return {
        "total": sum(counts.values()),
        "open": counts.get(SuggestionStatus.OPEN.value, 0),
        "under_review": counts.get(SuggestionStatus.UNDER_REVIEW.value, 0),
        "planned": counts.get(SuggestionStatus.PLANNED.value, 0),
        "implemented": counts.get(SuggestionStatus.IMPLEMENTED.value, 0),
        "declined": counts.get(SuggestionStatus.DECLINED.value, 0),
        "by_status": counts,
    }
