"""The Lounge.

Short polling, not a persistent connection: the client asks for messages newer
than the highest id it holds, every few seconds. That keeps the feature working
on a single shared worker and degrades gracefully if a request is dropped.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.lounge import LoungePost
from app.models.task import Task
from app.permissions import can_view_task, is_ceo, login_required
from app.services import lounge_service, settings_service, upload_service

bp = Blueprint("lounge", __name__)


@bp.route("/")
@login_required
def index():
    """The Lounge. Relaxed spacing, same design language as the rest."""
    topic = request.args.get("topic", "general")
    messages = lounge_service.recent_messages(viewer=current_user, topic=topic, limit=40)

    poll_seconds = settings_service.get_int("lounge_poll_seconds", 15)
    poll_seconds = max(10, min(poll_seconds, 60))

    return render_template(
        "lounge/index.html",
        page_title="The Lounge",
        messages=messages,
        topic=topic,
        topics=lounge_service.TOPICS,
        topic_counts=lounge_service.topic_counts(),
        poll_seconds=poll_seconds,
        latest_id=messages[-1]["id"] if messages else 0,
        stats=lounge_service.stats(),
        tasks=_referenceable_tasks(),
        max_files=current_app.config.get("MAX_FILES_PER_SUBMISSION", 3),
        max_upload_mb=current_app.config.get("MAX_UPLOAD_MB", 5),
    )


def _referenceable_tasks():
    from app.services.task_service import visible_tasks_query

    return (
        visible_tasks_query(current_user, include_completed=False)
        .order_by(Task.updated_at.desc())
        .limit(30)
        .all()
    )


@bp.route("/post", methods=["POST"])
@login_required
@limiter.limit("20 per minute;300 per hour")
def post():
    """Post a message. Rate limited per member, enforced server-side."""
    parent = None
    parent_id = request.form.get("parent_id", type=int)
    if parent_id:
        parent = db.session.get(LoungePost, parent_id)
        if parent is None or parent.is_hidden:
            abort(404)

    task = None
    task_id = request.form.get("task_id", type=int)
    if task_id:
        task = db.session.get(Task, task_id)
        if task is None or not can_view_task(current_user, task):
            abort(404)

    try:
        message = lounge_service.post_message(
            actor=current_user,
            body=request.form.get("body", ""),
            topic=request.form.get("topic"),
            parent=parent,
            task=task,
        )
    except lounge_service.LoungeError as exc:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("lounge.index"))

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if files:
        _attach_images(message, files)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(
            {
                "ok": True,
                "message": {
                    "id": message.id,
                    "author_name": current_user.display_name,
                    "author_initials": current_user.initials,
                    "body": message.body,
                    "topic": message.topic,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                    "parent_id": message.parent_id,
                    "reply_count": 0,
                    "is_edited": False,
                    "is_hidden": False,
                    "can_edit": True,
                    "can_delete": True,
                    "task_id": message.task_id,
                },
            }
        )

    return redirect(url_for("lounge.index", topic=message.topic) + f"#m{message.id}")


def _attach_images(message: LoungePost, files) -> None:
    """Store Lounge images under the same rules as task evidence."""
    max_files = int(current_app.config.get("MAX_FILES_PER_SUBMISSION", 3))
    stored = []
    for file in files[:max_files]:
        try:
            attachment = upload_service.validate_and_store(
                file, user=current_user, task_id=message.task_id
            )
            stored.append(attachment)
        except upload_service.UploadError as exc:
            flash(f"A file was not attached: {exc}", "error")
        except Exception:
            current_app.logger.exception("Lounge upload failed")
            flash("A file could not be attached.", "error")

    if stored:
        message.body = message.body + "\n\n" + "\n".join(
            f"[file #{row.id}: {row.original_name}]" for row in stored
        )
        db.session.commit()


@bp.route("/api/messages")
@login_required
@limiter.limit("240 per hour")
def api_messages():
    """Incremental fetch for polling.

    ``after`` returns newer messages; ``before`` pages backwards through
    history. Both are bounded, so a client can never pull the whole room in one
    request.
    """
    after_id = request.args.get("after", type=int)
    before_id = request.args.get("before", type=int)
    topic = request.args.get("topic", "general")
    limit = request.args.get("limit", default=40, type=int)

    try:
        payload = lounge_service.fetch_messages(
            viewer=current_user,
            after_id=after_id,
            before_id=before_id,
            topic=topic,
            limit=limit,
        )
    except Exception:
        current_app.logger.exception("Lounge polling failed")
        return jsonify({"ok": False, "error": "poll_failed"}), 500

    payload["ok"] = True
    payload["poll_seconds"] = max(10, min(settings_service.get_int("lounge_poll_seconds", 15), 60))
    return jsonify(payload)


@bp.route("/<int:message_id>/edit", methods=["POST"])
@login_required
def edit(message_id: int):
    message = db.session.get(LoungePost, message_id)
    if message is None:
        abort(404)

    try:
        lounge_service.edit_message(message, actor=current_user, body=request.form.get("body", ""))
    except lounge_service.LoungeError as exc:
        flash(str(exc), "error")
    else:
        flash("Message updated.", "success")
    return redirect(url_for("lounge.index", topic=message.topic) + f"#m{message.id}")


@bp.route("/<int:message_id>/delete", methods=["POST"])
@login_required
def delete(message_id: int):
    message = db.session.get(LoungePost, message_id)
    if message is None:
        abort(404)

    try:
        lounge_service.delete_message(message, actor=current_user)
    except lounge_service.LoungeError as exc:
        flash(str(exc), "error")
    else:
        flash("Message removed.", "info")
    return redirect(url_for("lounge.index", topic=message.topic))
