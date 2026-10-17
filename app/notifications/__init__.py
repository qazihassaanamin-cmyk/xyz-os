"""Notifications: the bell, the list, and marking as read."""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models.notification import Notification
from app.permissions import login_required
from app.services import notification_service
from app.utils.pagination import paginate

bp = Blueprint("notifications", __name__)


@bp.route("/")
@login_required
def index():
    """Full list, newest first, with an unread filter."""
    unread_only = request.args.get("unread") == "1"
    query = Notification.query.filter_by(user_id=current_user.id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc())

    pagination = paginate(query, per_page=30)
    return render_template(
        "notifications/index.html",
        page_title="Notifications",
        pagination=pagination,
        notifications=pagination.items,
        unread_only=unread_only,
        unread_count=notification_service.unread_count_for(current_user),
    )


@bp.route("/recent")
@login_required
def recent():
    """Small JSON payload for the bell dropdown."""
    rows = notification_service.recent_for(current_user, limit=12)
    return jsonify(
        {
            "unread": notification_service.unread_count_for(current_user),
            "items": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "icon": row.icon,
                    "title": row.title,
                    "body": row.body,
                    "url": row.url,
                    "is_unread": row.is_unread,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        }
    )


@bp.route("/<int:notification_id>/open", methods=["POST"])
@login_required
def open_notification(notification_id: int):
    """Mark read and follow the link, if the target is still reachable."""
    notification = _get_or_404(notification_id)
    notification_service.mark_read(notification)

    target = notification.url
    if target:
        return redirect(target)
    return redirect(url_for("notifications.index"))


@bp.route("/<int:notification_id>/read", methods=["POST"])
@login_required
def mark_read(notification_id: int):
    notification = _get_or_404(notification_id)
    notification_service.mark_read(notification)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(
            {
                "ok": True,
                "unread": notification_service.unread_count_for(current_user),
            }
        )
    return redirect(request.referrer or url_for("notifications.index"))


@bp.route("/read-all", methods=["POST"])
@login_required
def mark_all_read():
    count = notification_service.mark_all_read(current_user)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "marked": count, "unread": 0})
    return redirect(request.referrer or url_for("notifications.index"))


def _get_or_404(notification_id: int) -> Notification:
    # Scoped by owner, so one member can never address another's notification.
    notification = Notification.query.filter_by(
        id=notification_id, user_id=current_user.id
    ).first()
    if notification is None:
        abort(404)
    return notification
