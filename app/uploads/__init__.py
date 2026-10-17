"""Authorized file delivery.

There is no static path to any upload. Every byte of every file is served
through this blueprint after a policy check on the parent record, with
``nosniff`` set and PDFs forced to download rather than render.
"""

from __future__ import annotations

import os

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.attachment import Attachment
from app.permissions import can_view_task, is_ceo, login_required
from app.services import upload_service

bp = Blueprint("uploads", __name__)


def can_view_file(user, attachment: Attachment) -> bool:
    """The single authorization decision for every download.

    A file is visible when the member can see the record it is attached to.
    """
    if attachment is None or user is None or not getattr(user, "is_authenticated", False):
        return False
    if attachment.deleted_at is not None and not is_ceo(user):
        return False
    if is_ceo(user):
        return True
    if attachment.uploaded_by_id == user.id:
        return True
    if attachment.task is not None and can_view_task(user, attachment.task):
        return True
    if attachment.submission is not None and attachment.submission.task is not None:
        return can_view_task(user, attachment.submission.task)
    return False


def _get_attachment_or_404(attachment_id: int) -> Attachment:
    attachment = db.session.get(Attachment, attachment_id)
    # 404 rather than 403 for anything the member may not know exists.
    if attachment is None or not can_view_file(current_user, attachment):
        abort(404)
    return attachment


@bp.route("/<int:attachment_id>")
@login_required
def download(attachment_id: int):
    """Serve the original file. Always forces a download for non-images."""
    attachment = _get_attachment_or_404(attachment_id)
    path = upload_service.absolute_path(attachment)
    if path is None or not os.path.isfile(path):
        current_app.logger.warning("Attachment %s is missing from disk", attachment.id)
        abort(404)

    response = send_file(
        path,
        mimetype=attachment.content_type,
        as_attachment=not attachment.is_image,
        download_name=attachment.original_name,
        conditional=True,
        max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store, private"
    if attachment.content_type == "application/pdf":
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{attachment.original_name}"'
        )
    return response


@bp.route("/<int:attachment_id>/thumbnail")
@login_required
def thumbnail(attachment_id: int):
    """Serve the server-generated thumbnail, falling back to the original."""
    attachment = _get_attachment_or_404(attachment_id)
    if not attachment.is_image:
        abort(404)

    thumb = upload_service.thumbnail_path(attachment)
    if thumb is None or not os.path.isfile(thumb):
        return redirect(url_for("uploads.download", attachment_id=attachment.id))

    response = send_file(
        thumb,
        mimetype="image/jpeg",
        as_attachment=False,
        max_age=3600,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/<int:attachment_id>/preview")
@login_required
def preview(attachment_id: int):
    """Inline view of an image for the lightbox."""
    attachment = _get_attachment_or_404(attachment_id)
    if not attachment.is_image:
        return redirect(url_for("uploads.download", attachment_id=attachment.id))

    path = upload_service.absolute_path(attachment)
    if path is None or not os.path.isfile(path):
        abort(404)

    response = send_file(path, mimetype=attachment.content_type, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store, private"
    return response


@bp.route("/<int:attachment_id>/caption", methods=["POST"])
@login_required
def update_caption(attachment_id: int):
    """Add or correct the caption on an evidence file."""
    attachment = _get_attachment_or_404(attachment_id)
    if not (
        is_ceo(current_user)
        or attachment.uploaded_by_id == current_user.id
    ):
        abort(403)

    caption = (request.form.get("caption") or "").strip()[:300] or None
    attachment.caption = caption
    db.session.commit()

    from app.services.audit_service import record_audit

    record_audit(
        "upload.caption_updated",
        entity_type="attachment",
        entity_id=attachment.id,
        summary=f"Caption updated for {attachment.original_name}",
        after={"caption": caption},
        user_id=current_user.id,
    )
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.route("/<int:attachment_id>/delete", methods=["POST"])
@login_required
def delete(attachment_id: int):
    """Soft-delete an incorrect upload. Never a hard delete from here."""
    attachment = _get_attachment_or_404(attachment_id)
    try:
        upload_service.soft_delete(attachment, user=current_user)
    except upload_service.UploadError as exc:
        abort(403, description=str(exc))
    return redirect(request.referrer or url_for("dashboard.index"))


# ---------------------------------------------------------------------------
# Storage administration (CEO)
# ---------------------------------------------------------------------------

@bp.route("/storage")
@login_required
def storage():
    """Storage usage and the purge queue."""
    if not is_ceo(current_user):
        abort(403)

    usage = upload_service.storage_usage()
    candidates = upload_service.purge_candidates(older_than_days=0, limit=100)
    return render_template(
        "uploads/storage.html",
        page_title="File storage",
        usage=usage,
        candidates=candidates,
    )


@bp.route("/storage/purge", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def purge():
    """Permanently remove already soft-deleted files past the retention window."""
    if not is_ceo(current_user):
        abort(403)

    retention = int(current_app.config.get("UPLOAD_RETENTION_DAYS", 30))
    removed = upload_service.purge_expired(older_than_days=retention, actor=current_user)
    return redirect(url_for("uploads.storage", purged=removed))


@bp.route("/storage/purge/<int:attachment_id>", methods=["POST"])
@login_required
def purge_one(attachment_id: int):
    """Purge one specific removed file."""
    if not is_ceo(current_user):
        abort(403)

    attachment = db.session.get(Attachment, attachment_id)
    if attachment is None:
        abort(404)

    try:
        upload_service.purge_attachment(attachment, user=current_user)
    except upload_service.UploadError as exc:
        abort(400, description=str(exc))
    return redirect(url_for("uploads.storage"))
