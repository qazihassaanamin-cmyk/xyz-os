"""File attachments.

Files are never stored inside the static directory and are never served by a
static URL. Each download goes through a route that performs an authorization
check against the parent record first. Stored filenames are random; the original
name is kept only for display and for the download's Content-Disposition.
"""

from __future__ import annotations

import os

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin, utcnow

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf", ".txt", ".md", ".csv", ".json", ".zip", ".doc", ".docx", ".xls", ".xlsx"}


class Attachment(TimestampMixin, db.Model):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Exactly one of these parents is set. Enforced in the service layer.
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Random name on disk. Never derived from user input.
    stored_name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Avatar images are profile pictures, not task evidence.
    is_avatar: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False)

    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])
    task = relationship("Task", back_populates="attachments", foreign_keys=[task_id])
    submission = relationship("Submission", back_populates="attachments", foreign_keys=[submission_id])

    __table_args__ = (
        Index("ix_attachments_task", "task_id"),
        Index("ix_attachments_submission", "submission_id"),
    )

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def size_label(self) -> str:
        size = float(self.size_bytes or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    @property
    def extension(self) -> str:
        return os.path.splitext(self.original_name or "")[1].lower()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attachment {self.id} {self.original_name!r}>"
