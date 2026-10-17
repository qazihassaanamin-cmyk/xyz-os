"""Private file uploads.

Design constraints, in order of importance:

* Nothing is trusted from the client — not the filename, not the declared MIME
  type, not the extension.
* Images are re-encoded with Pillow, which strips EXIF and any embedded
  payload, and produces a server-generated thumbnail.
* Files live outside the static directory and are only ever served by an
  authorized route.
* The PythonAnywhere free tier allows 512 MiB total, so uploads are small and
  the per-member and global usage is reported.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import uuid
from datetime import datetime, timedelta

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.attachment import Attachment
from app.models.base import utcnow
from app.services.audit_service import record_audit

log = logging.getLogger(__name__)

IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}
PDF_MAGIC = b"%PDF-"

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_FILES = 3
# Generous enough for a document, small enough that a decompression bomb cannot
# exhaust memory on a shared free-tier worker.
MAX_PIXELS = 24_000_000
THUMBNAIL_SIZE = (400, 400)
AVATAR_SIZE = (256, 256)

DELETABLE_WINDOW = timedelta(minutes=15)


class UploadError(Exception):
    """A rejected upload, with a message safe to show the member."""


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def max_bytes() -> int:
    return int(current_app.config.get("MAX_UPLOAD_MB", 5)) * 1024 * 1024 or DEFAULT_MAX_BYTES


def max_files() -> int:
    return int(current_app.config.get("MAX_FILES_PER_SUBMISSION", DEFAULT_MAX_FILES))


def upload_root() -> str:
    return current_app.config["UPLOAD_DIR_ABS"]


def _month_dir(kind: str = "submissions") -> str:
    now = datetime.utcnow()
    path = os.path.join(upload_root(), kind, str(now.year), f"{now.month:02d}")
    os.makedirs(path, exist_ok=True)
    return path


def _thumb_dir() -> str:
    path = os.path.join(upload_root(), "thumbnails")
    os.makedirs(path, exist_ok=True)
    return path


def _avatar_dir() -> str:
    path = os.path.join(upload_root(), "avatars")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Storage accounting
# ---------------------------------------------------------------------------

def _safe_walk_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def storage_usage() -> dict:
    """Disk usage of the upload area, for the CEO's storage panel."""
    root = upload_root()
    uploads = _safe_walk_size(os.path.join(root, "submissions")) if os.path.isdir(
        os.path.join(root, "submissions")
    ) else 0
    avatars = _safe_walk_size(os.path.join(root, "avatars")) if os.path.isdir(
        os.path.join(root, "avatars")
    ) else 0
    thumbs = _safe_walk_size(os.path.join(root, "thumbnails")) if os.path.isdir(
        os.path.join(root, "thumbnails")
    ) else 0

    db_size = 0
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite:///"):
        db_path = uri.replace("sqlite:///", "", 1)
        if not os.path.isabs(db_path):
            db_path = os.path.join(current_app.root_path, "..", db_path)
        try:
            db_size = os.path.getsize(os.path.abspath(db_path))
        except OSError:
            db_size = 0

    total = uploads + avatars + thumbs + db_size
    return {
        "uploads_bytes": uploads,
        "avatars_bytes": avatars,
        "thumbnails_bytes": thumbs,
        "database_bytes": db_size,
        "total_bytes": total,
        "budget_bytes": 512 * 1024 * 1024,
        "percent_of_budget": round(100 * total / (512 * 1024 * 1024), 1),
    }


def usage_for_user(user) -> int:
    total = (
        db.session.query(db.func.coalesce(db.func.sum(Attachment.size_bytes), 0))
        .filter(Attachment.uploaded_by_id == user.id, Attachment.deleted_at.is_(None))
        .scalar()
    )
    return int(total or 0)


def per_user_limit_bytes() -> int:
    return int(current_app.config.get("PER_USER_UPLOAD_LIMIT_MB", 60)) * 1024 * 1024


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _sniff_kind(blob: bytes) -> str | None:
    """Identify real content from magic bytes. Returns 'image', 'pdf' or None."""
    if len(blob) < 12:
        return None

    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "image"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image"
    if blob.startswith(PDF_MAGIC):
        return "pdf"
    return None


def _clean_filename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    base = secure_filename(base) or "file"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if len(base) > 120:
        stem, ext = os.path.splitext(base)
        base = stem[:110] + ext
    return base or "file"


def validate_and_store(
    file: FileStorage,
    *,
    user,
    task_id: int | None = None,
    submission_id: int | None = None,
    caption: str | None = None,
    is_avatar: bool = False,
) -> Attachment:
    """Validate real content, re-encode if it is an image, and store it."""
    if file is None or not getattr(file, "filename", ""):
        raise UploadError("Choose a file to upload.")

    limit = max_bytes()
    blob = file.read(limit + 1)
    if not blob:
        raise UploadError("That file is empty.")
    if len(blob) > limit:
        raise UploadError(f"Files must be {limit // (1024 * 1024)} MB or smaller.")

    kind = _sniff_kind(blob)
    if kind is None:
        raise UploadError(
            "Only PNG, JPEG, GIF, WebP images and PDF documents are accepted."
        )

    original_name = _clean_filename(file.filename)
    ext = os.path.splitext(original_name)[1].lower()

    if kind == "image":
        return _store_image(
            blob,
            original_name=original_name,
            user=user,
            task_id=task_id,
            submission_id=submission_id,
            caption=caption,
            is_avatar=is_avatar,
        )
    if kind == "pdf":
        if ext and ext != ".pdf":
            # A PDF that claims to be something else is still stored as a PDF.
            original_name = os.path.splitext(original_name)[0] + ".pdf"
        return _store_pdf(
            blob,
            original_name=original_name,
            user=user,
            task_id=task_id,
            submission_id=submission_id,
            caption=caption,
        )
    raise UploadError("Unsupported file type.")


def _store_image(
    blob: bytes,
    *,
    original_name: str,
    user,
    task_id,
    submission_id,
    caption,
    is_avatar: bool,
) -> Attachment:
    try:
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with Image.open(io.BytesIO(blob)) as probe:
            probe.verify()
        # verify() invalidates the object, so reopen to actually process it.
        with Image.open(io.BytesIO(blob)) as source:
            source = ImageOps.exif_transpose(source)
            width, height = source.size
            if width * height > MAX_PIXELS:
                raise UploadError("That image has too many pixels to process.")

            has_alpha = source.mode in ("RGBA", "LA", "P")
            target_mode = "RGBA" if has_alpha else "RGB"
            if source.mode not in ("RGB", target_mode):
                source = source.convert(target_mode)

            buffer = io.BytesIO()
            if has_alpha:
                source.save(buffer, format="PNG", optimize=True)
                stored_ext = ".png"
                content_type = "image/png"
            else:
                source.save(buffer, format="JPEG", quality=85, optimize=True, progressive=True)
                stored_ext = ".jpg"
                content_type = "image/jpeg"
            payload = buffer.getvalue()

            thumb = source.copy()
            thumb.thumbnail(THUMBNAIL_SIZE if not is_avatar else AVATAR_SIZE, Image.LANCZOS)
            thumb_buffer = io.BytesIO()
            if thumb.mode == "RGBA":
                thumb.save(thumb_buffer, format="PNG", optimize=True)
            else:
                thumb.save(thumb_buffer, format="JPEG", quality=80, optimize=True)
            thumb_payload = thumb_buffer.getvalue()
    except UploadError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.info("Rejected image upload from user %s: %s", getattr(user, "id", None), exc)
        raise UploadError("That image could not be read. It may be corrupt or mislabelled.")

    stored_name = f"{uuid.uuid4().hex}{stored_ext}"
    directory = _avatar_dir() if is_avatar else _month_dir("submissions")
    _check_quota(user, len(payload))
    _write(os.path.join(directory, stored_name), payload)

    thumb_name = f"{os.path.splitext(stored_name)[0]}_thumb.jpg"
    _write(os.path.join(_thumb_dir(), thumb_name), thumb_payload)

    attachment = Attachment(
        uploaded_by_id=user.id,
        task_id=task_id,
        submission_id=submission_id,
        stored_name=stored_name,
        original_name=original_name,
        content_type=content_type,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        caption=(caption or "").strip()[:300] or None,
        is_avatar=is_avatar,
    )
    _persist(attachment, user)
    return attachment


def _store_pdf(
    blob: bytes,
    *,
    original_name: str,
    user,
    task_id,
    submission_id,
    caption,
) -> Attachment:
    stored_name = f"{uuid.uuid4().hex}.pdf"
    _check_quota(user, len(blob))
    _write(os.path.join(_month_dir("submissions"), stored_name), blob)

    attachment = Attachment(
        uploaded_by_id=user.id,
        task_id=task_id,
        submission_id=submission_id,
        stored_name=stored_name,
        original_name=original_name,
        content_type="application/pdf",
        size_bytes=len(blob),
        sha256=hashlib.sha256(blob).hexdigest(),
        caption=(caption or "").strip()[:300] or None,
    )
    _persist(attachment, user)
    return attachment


def _check_quota(user, incoming_bytes: int) -> None:
    used = usage_for_user(user)
    limit = per_user_limit_bytes()
    if used + incoming_bytes > limit:
        raise UploadError(
            f"You have used {used // (1024 * 1024)} MB of your "
            f"{limit // (1024 * 1024)} MB upload allowance. "
            "Remove an older file before adding another."
        )


def _write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # Write to a temporary file first so a partial write never becomes visible.
    temp_path = f"{path}.part"
    with open(temp_path, "wb") as handle:
        handle.write(payload)
    os.replace(temp_path, path)


def _persist(attachment: Attachment, user) -> Attachment:
    db.session.add(attachment)
    db.session.commit()
    record_audit(
        "upload.stored",
        entity_type="attachment",
        entity_id=attachment.id,
        summary=f"File uploaded: {attachment.original_name}",
        after={
            "size_bytes": attachment.size_bytes,
            "content_type": attachment.content_type,
            "task_id": attachment.task_id,
            "submission_id": attachment.submission_id,
        },
        user_id=getattr(user, "id", None),
    )
    return attachment


# ---------------------------------------------------------------------------
# Retrieval and removal
# ---------------------------------------------------------------------------

def absolute_path(attachment: Attachment) -> str | None:
    for directory in (
        _avatar_dir(),
        _thumb_dir(),
        os.path.join(upload_root(), "submissions"),
    ):
        if directory.endswith("submissions"):
            for root, _dirs, files in os.walk(directory):
                if attachment.stored_name in files:
                    return os.path.join(root, attachment.stored_name)
            continue
        candidate = os.path.join(directory, attachment.stored_name)
        if os.path.isfile(candidate):
            return candidate
    return None


def thumbnail_path(attachment: Attachment) -> str | None:
    stem = os.path.splitext(attachment.stored_name)[0]
    candidate = os.path.join(_thumb_dir(), f"{stem}_thumb.jpg")
    return candidate if os.path.isfile(candidate) else None


def can_delete(attachment: Attachment, user) -> bool:
    """The uploader within a short window, or the CEO at any time."""
    if attachment.deleted_at is not None:
        return False
    if user is None or getattr(user, "id", None) is None:
        return False
    if getattr(user, "is_ceo", False):
        return True
    if attachment.uploaded_by_id != user.id:
        return False
    if attachment.created_at is None:
        return False
    created = attachment.created_at
    if created.tzinfo is None:
        from datetime import timezone

        created = created.replace(tzinfo=timezone.utc)
    return (utcnow() - created) <= DELETABLE_WINDOW


def soft_delete(attachment: Attachment, *, user) -> None:
    if not can_delete(attachment, user):
        raise UploadError(
            "That file can no longer be removed here. Ask the CEO to delete it."
        )
    attachment.deleted_at = utcnow()
    attachment.deleted_by_id = user.id
    db.session.commit()

    record_audit(
        "upload.soft_deleted",
        entity_type="attachment",
        entity_id=attachment.id,
        summary=f"File removed: {attachment.original_name}",
        user_id=getattr(user, "id", None),
    )


def purge_attachment(attachment: Attachment, *, user) -> bool:
    """Permanently remove one already soft-deleted file. CEO only."""
    if attachment.deleted_at is None:
        raise UploadError("Remove the file before purging it.")
    path = absolute_path(attachment)
    removed = False
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            removed = True
        except OSError:
            log.exception("Could not purge file %s", path)
    thumb = thumbnail_path(attachment)
    if thumb and os.path.isfile(thumb):
        try:
            os.remove(thumb)
        except OSError:
            pass

    record_audit(
        "upload.purged",
        entity_type="attachment",
        entity_id=attachment.id,
        summary=f"File purged from storage: {attachment.original_name}",
        after={"size_bytes": attachment.size_bytes, "file_removed": removed},
        user_id=getattr(user, "id", None),
    )
    db.session.delete(attachment)
    db.session.commit()
    return removed


def purge_candidates(*, older_than_days: int = 30, limit: int = 50) -> list[Attachment]:
    """Soft-deleted files eligible for permanent removal."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    return (
        Attachment.query.filter(
            Attachment.deleted_at.isnot(None), Attachment.deleted_at <= cutoff
        )
        .order_by(Attachment.deleted_at.asc())
        .limit(limit)
        .all()
    )


def purge_expired(*, older_than_days: int = 30, actor=None) -> int:
    """Purge soft-deleted files past the retention window. Never touches live files."""
    count = 0
    for attachment in purge_candidates(older_than_days=older_than_days):
        try:
            purge_attachment(attachment, user=actor)
            count += 1
        except Exception:  # pragma: no cover
            log.exception("Purge failed for attachment %s", attachment.id)
    return count


# ---------------------------------------------------------------------------
# Galleries
# ---------------------------------------------------------------------------

def gallery_for_task(task, *, include_deleted: bool = False) -> list[Attachment]:
    query = Attachment.query.filter_by(task_id=task.id)
    if not include_deleted:
        query = query.filter(Attachment.deleted_at.is_(None))
    return query.order_by(Attachment.created_at.asc()).all()


def gallery_for_submission(submission, *, include_deleted: bool = False) -> list[Attachment]:
    query = Attachment.query.filter_by(submission_id=submission.id)
    if not include_deleted:
        query = query.filter(Attachment.deleted_at.is_(None))
    return query.order_by(Attachment.created_at.asc()).all()


def images_for_task(task) -> list[Attachment]:
    return [row for row in gallery_for_task(task) if row.is_image]
