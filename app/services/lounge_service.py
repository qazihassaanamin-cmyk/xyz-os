"""The Lounge.

An internal conversation space. Messages are persisted before anything is
returned to a client, edits are windowed and keep the previous text, and every
message is escaped on output — the templates never mark user content as safe.

Polling is short and incremental: a client asks for messages newer than the
highest id it holds. No long-lived connection is required, which keeps the
feature working on shared hosting with a single worker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.extensions import db
from app.models.base import utcnow
from app.models.lounge import EDIT_WINDOW_SECONDS, LoungePost, LoungePostRevision
from app.services.audit_service import record_audit
from app.utils.validators import require_text

log = logging.getLogger(__name__)

MAX_BODY = 4000
PAGE_SIZE = 40
MAX_PAGE_SIZE = 100
TOPICS = ["general", "tech", "wins", "random"]


class LoungeError(Exception):
    """A refused Lounge operation, with a member-safe message."""


def visible_query(*, room_id=None, include_hidden: bool = False):
    query = LoungePost.query.filter(LoungePost.parent_id.is_(None))
    if not include_hidden:
        query = query.filter(LoungePost.is_hidden.is_(False))
    if room_id is not None:
        query = query.filter(LoungePost.room_id == room_id)
    return query


def _normalise_topic(topic: str | None) -> str:
    value = (topic or "general").strip().lower()
    return value if value in TOPICS else "general"


def post_message(
    *,
    actor,
    body: str,
    topic: str | None = None,
    parent: LoungePost | None = None,
    task=None,
) -> LoungePost:
    """Persist a message. Replies inherit their parent's topic."""
    body = require_text(body, field="Message", min_length=1, max_length=MAX_BODY)

    resolved_topic = _normalise_topic(topic)
    if parent is not None:
        if parent.is_hidden:
            raise LoungeError("That message is no longer available.")
        if parent.parent_id is not None:
            raise LoungeError("Replies are one level deep. Reply to the original message.")
        resolved_topic = parent.topic

    if task is not None:
        from app.permissions import can_view_task

        if not can_view_task(actor, task):
            raise LoungeError("You cannot reference that task.")

    message = LoungePost(
        author_id=actor.id,
        body=body,
        topic=resolved_topic,
        parent_id=parent.id if parent else None,
        task_id=task.id if task else None,
    )
    db.session.add(message)

    if parent is not None:
        parent.reply_count = (parent.reply_count or 0) + 1

    db.session.commit()
    return message


def edit_message(message: LoungePost, *, actor, body: str) -> LoungePost:
    """Edit within the allowed window, keeping the previous text."""
    if not message.is_editable_by(actor):
        raise LoungeError(
            "Messages can only be edited by their author within "
            f"{EDIT_WINDOW_SECONDS // 60} minutes of posting."
        )

    body = require_text(body, field="Message", min_length=1, max_length=MAX_BODY)
    if body == message.body:
        return message

    db.session.add(
        LoungePostRevision(
            post_id=message.id,
            previous_body=message.body,
            edited_at=utcnow(),
        )
    )
    message.body = body
    message.edited_at = utcnow()
    db.session.commit()

    record_audit(
        "lounge.message_edited",
        entity_type="lounge_post",
        entity_id=message.id,
        summary="Lounge message edited by its author",
        user_id=actor.id,
    )
    return message


def delete_message(message: LoungePost, *, actor) -> LoungePost:
    """Remove a message. The author may always remove their own; the CEO may
    moderate any message, and a moderation removal records who did it."""
    from app.permissions import is_ceo

    is_author = message.author_id == getattr(actor, "id", None)
    if not (is_author or is_ceo(actor)):
        raise LoungeError("You can only remove your own messages.")

    message.is_hidden = True
    message.hidden_by_id = actor.id
    message.hidden_reason = "Removed by author" if is_author else "Removed by the CEO"
    if message.parent is not None and message.parent.reply_count:
        message.parent.reply_count = max(0, message.parent.reply_count - 1)
    db.session.commit()

    record_audit(
        "lounge.message_removed",
        entity_type="lounge_post",
        entity_id=message.id,
        summary=(
            "Lounge message removed by its author"
            if is_author
            else "Lounge message removed by the CEO"
        ),
        user_id=actor.id,
    )
    return message


def _serialise(message: LoungePost, *, viewer, include_replies: bool = False) -> dict:
    from app.permissions import is_ceo

    can_moderate = is_ceo(viewer)
    return {
        "id": message.id,
        "author_id": message.author_id,
        "author_name": message.author.display_name if message.author else "Former member",
        "author_initials": message.author.initials if message.author else "?",
        "author_is_ceo": bool(message.author and message.author.is_ceo),
        "body": message.body,
        "topic": message.topic,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "is_edited": message.is_edited,
        "is_hidden": message.is_hidden,
        "parent_id": message.parent_id,
        "reply_count": message.reply_count or 0,
        "task_id": message.task_id,
        "can_edit": message.is_editable_by(viewer),
        "can_delete": bool(
            (message.author_id == getattr(viewer, "id", None) or can_moderate)
            and not message.is_hidden
        ),
    }


def fetch_messages(
    *, viewer, after_id: int | None = None, before_id: int | None = None,
    topic: str | None = None, limit: int = PAGE_SIZE, with_replies: bool = True,
) -> dict:
    """Incremental fetch for the polling endpoint.

    ``after_id`` returns newer messages (the common case); ``before_id`` pages
    backwards through history. The result is always bounded.
    """
    limit = max(1, min(int(limit or PAGE_SIZE), MAX_PAGE_SIZE))
    include_hidden = viewer is not None and getattr(viewer, "is_ceo", False)

    query = visible_query(include_hidden=include_hidden)
    if topic:
        query = query.filter(LoungePost.topic == topic)

    if after_id:
        query = query.filter(LoungePost.id > int(after_id)).order_by(LoungePost.id.asc())
        rows = query.limit(limit).all()
    elif before_id:
        query = query.filter(LoungePost.id < int(before_id)).order_by(LoungePost.id.desc())
        rows = list(reversed(query.limit(limit).all()))
    else:
        query = query.order_by(LoungePost.id.desc())
        rows = list(reversed(query.limit(limit).all()))

    payload = [_serialise(row, viewer=viewer) for row in rows]

    if with_replies and payload:
        ids = [row["id"] for row in payload]
        replies = (
            visible_query(include_hidden=include_hidden)
            .filter(LoungePost.parent_id.in_(ids))
            .order_by(LoungePost.id.asc())
            .limit(limit * 3)
            .all()
        )
        for reply in replies:
            payload.append(_serialise(reply, viewer=viewer))
        payload.sort(key=lambda item: item["id"])

    return {
        "messages": payload,
        "latest_id": payload[-1]["id"] if payload else (after_id or 0),
        "count": len(payload),
        "has_more": len(payload) >= limit,
        "server_time": utcnow().isoformat(),
    }


def recent_messages(*, viewer, topic: str | None = None, limit: int = PAGE_SIZE) -> list[dict]:
    """The most recent messages, oldest first, for a full page render."""
    data = fetch_messages(viewer=viewer, topic=topic, limit=limit)
    return data["messages"]


def topic_counts() -> dict[str, int]:
    rows = (
        db.session.query(LoungePost.topic, db.func.count(LoungePost.id))
        .filter(LoungePost.parent_id.is_(None), LoungePost.is_hidden.is_(False))
        .group_by(LoungePost.topic)
        .all()
    )
    counts = {topic: 0 for topic in TOPICS}
    for topic, count in rows:
        if topic in counts:
            counts[topic] = count
    return counts


def stats() -> dict:
    total = LoungePost.query.filter_by(is_hidden=False).count()
    today_start = datetime.combine(utcnow().date(), datetime.min.time(), tzinfo=timezone.utc)
    today = LoungePost.query.filter(
        LoungePost.is_hidden.is_(False), LoungePost.created_at >= today_start
    ).count()
    return {"total_messages": total, "messages_today": today, "topics": len(TOPICS)}
