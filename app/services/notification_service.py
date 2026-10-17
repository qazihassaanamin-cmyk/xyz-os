"""In-app notifications.

``notify`` is idempotent: the unique ``dedupe_key`` means the same event can be
raised from several code paths without producing duplicate notices. Email is
optional and never required for the application to function.
"""

from __future__ import annotations

import logging

from flask import current_app, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import utcnow
from app.models.notification import Notification
from app.services.audit_service import record_audit

log = logging.getLogger(__name__)


def notify(
    user,
    kind: str,
    *,
    title: str,
    body: str | None = None,
    link_endpoint: str | None = None,
    link_params: str | None = None,
    subject_type: str | None = None,
    subject_id: int | None = None,
    dedupe_key: str | None = None,
    actor=None,
    email: bool = False,
) -> Notification | None:
    """Create a notification unless an identical one already exists."""
    if user is None or getattr(user, "id", None) is None:
        return None
    # Never notify someone about their own action.
    if actor is not None and getattr(actor, "id", None) == user.id:
        return None

    if dedupe_key:
        existing = Notification.query.filter_by(dedupe_key=dedupe_key).first()
        if existing is not None:
            return existing

    row = Notification(
        user_id=user.id,
        kind=kind,
        title=title[:200],
        body=body,
        link_endpoint=link_endpoint,
        link_params=link_params,
        subject_type=subject_type,
        subject_id=subject_id,
        dedupe_key=dedupe_key,
        actor_id=getattr(actor, "id", None),
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if dedupe_key:
            return Notification.query.filter_by(dedupe_key=dedupe_key).first()
        return None

    if email:
        _maybe_email(user, row)
    return row


def _maybe_email(user, row: Notification) -> None:
    """Send an email copy only when SMTP is configured."""
    if not current_app.config.get("SMTP_HOST"):
        return
    try:
        from app.services.mail_service import send_notification_email

        link = row.url or url_for("dashboard.index", _external=True)
        sent = send_notification_email(user, row.title, row.body or row.title, link)
        if sent:
            row.emailed_at = utcnow()
            db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Notification email failed for user %s", user.id)


def notify_many(users, kind: str, **kwargs) -> list[Notification]:
    created = []
    for user in users:
        row = notify(user, kind, **kwargs)
        if row is not None:
            created.append(row)
    return created


def unread_count_for(user) -> int:
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return Notification.query.filter_by(user_id=user.id, read_at=None).count()


def recent_for(user, *, limit: int = 12, unread_only: bool = False) -> list[Notification]:
    query = Notification.query.filter_by(user_id=user.id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    return query.order_by(Notification.created_at.desc()).limit(limit).all()


def mark_read(notification: Notification) -> None:
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.session.commit()


def mark_all_read(user) -> int:
    count = (
        Notification.query.filter_by(user_id=user.id, read_at=None).update(
            {"read_at": utcnow()}, synchronize_session=False
        )
    )
    db.session.commit()
    return int(count or 0)


# ---------------------------------------------------------------------------
# Event-specific helpers. Kept here so routes do not construct titles inline.
# ---------------------------------------------------------------------------

def task_assigned(task, actor=None) -> None:
    if task.assignee is None:
        return
    notify(
        task.assignee,
        "TASK_ASSIGNED",
        title=f"Assigned: {task.title}",
        body=_task_context(task),
        link_endpoint="tasks.detail",
        link_params=f"task_id={task.id}",
        subject_type="task",
        subject_id=task.id,
        dedupe_key=f"task-assigned:{task.id}:{task.assignee_id}:{task.updated_at}",
        actor=actor,
    )


def submission_received(submission, actor=None) -> None:
    task = submission.task
    if task is None:
        return
    recipient = task.reviewer or _ceo_user()
    if recipient is None:
        return
    notify(
        recipient,
        "SUBMISSION_RECEIVED",
        title=f"Review requested: {task.title}",
        body=(submission.summary or "")[:300],
        link_endpoint="tasks.detail",
        link_params=f"task_id={task.id}",
        subject_type="submission",
        subject_id=submission.id,
        dedupe_key=f"submission:{submission.id}:received",
        actor=actor,
    )


def review_decision(submission, decision: str, feedback: str | None, actor=None) -> None:
    task = submission.task
    if task is None or task.assignee is None:
        return
    approved = decision == "APPROVED"
    notify(
        task.assignee,
        "REVIEW_APPROVED" if approved else "CHANGES_REQUESTED",
        title=(
            f"Approved: {task.title}" if approved else f"Changes requested: {task.title}"
        ),
        body=feedback,
        link_endpoint="tasks.detail",
        link_params=f"task_id={task.id}",
        subject_type="submission",
        subject_id=submission.id,
        dedupe_key=f"submission:{submission.id}:{decision.lower()}",
        actor=actor,
    )


def blocker_raised(blocker, actor=None) -> None:
    recipients = []
    if blocker.assigned_to is not None:
        recipients.append(blocker.assigned_to)
    if not recipients:
        ceo = _ceo_user()
        if ceo is not None:
            recipients.append(ceo)
    for recipient in recipients:
        notify(
            recipient,
            "BLOCKER_RAISED",
            title="A blocker needs attention",
            body=(blocker.description or "")[:300],
            link_endpoint="tasks.blockers",
            subject_type="blocker",
            subject_id=blocker.id,
            dedupe_key=f"blocker:{blocker.id}:raised:{recipient.id}",
            actor=actor,
        )


def blocker_resolved(blocker, actor=None) -> None:
    if blocker.raised_by is None:
        return
    notify(
        blocker.raised_by,
        "BLOCKER_RESOLVED",
        title="Your blocker was resolved",
        body=blocker.resolution_note,
        link_endpoint="tasks.blockers",
        subject_type="blocker",
        subject_id=blocker.id,
        dedupe_key=f"blocker:{blocker.id}:resolved",
        actor=actor,
    )


def comment_added(task, comment, actor=None) -> None:
    recipients = {task.assignee, task.reviewer, task.creator}
    recipients.discard(None)
    for recipient in recipients:
        notify(
            recipient,
            "COMMENT_ADDED",
            title=f"New comment on: {task.title}",
            body=(comment.body or "")[:300],
            link_endpoint="tasks.detail",
            link_params=f"task_id={task.id}",
            subject_type="task_comment",
            subject_id=comment.id,
            dedupe_key=f"comment:{comment.id}:{recipient.id}",
            actor=actor,
        )


def announcement_published(announcement, recipients, actor=None) -> None:
    for recipient in recipients:
        notify(
            recipient,
            "ANNOUNCEMENT",
            title=announcement.title,
            body=(announcement.body or "")[:300],
            link_endpoint="announcements.detail",
            link_params=f"announcement_id={announcement.id}",
            subject_type="announcement",
            subject_id=announcement.id,
            dedupe_key=f"announcement:{announcement.id}:{recipient.id}",
            actor=actor,
        )


def suggestion_status_changed(suggestion, new_status: str, actor=None) -> None:
    if suggestion.is_anonymous or suggestion.author is None:
        return
    notify(
        suggestion.author,
        "SUGGESTION_STATUS",
        title=f"Suggestion {new_status.lower().replace('_', ' ')}: {suggestion.title}",
        body=suggestion.decision_note,
        link_endpoint="suggestions.detail",
        link_params=f"suggestion_id={suggestion.id}",
        subject_type="suggestion",
        subject_id=suggestion.id,
        dedupe_key=f"suggestion:{suggestion.id}:{new_status}",
        actor=actor,
    )


def _task_context(task) -> str | None:
    parts = []
    if task.project is not None:
        parts.append(task.project.name)
    if task.due_date is not None:
        parts.append(f"due {task.due_date.isoformat()}")
    if task.priority and task.priority != "NORMAL":
        parts.append(f"{task.priority.title()} priority")
    return " · ".join(parts) or None


def _ceo_user():
    from app.models.user import User

    return User.query.filter_by(role="CEO", is_active_flag=True).first()
