"""Immutable audit trail of privileged actions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin, utcnow


class AuditLog(TimestampMixin, db.Model):
    """Append-only. No update or delete path exists in application code."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    user = relationship("User")

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_created", "created_at"),
    )

    @property
    def action_label(self) -> str:
        return self.action.replace("_", " ").replace(".", " › ").title()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.id} {self.action!r} user={self.user_id}>"
