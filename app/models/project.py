"""Project, membership and milestone models."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import StrEnum, TimestampMixin


class ProjectStatus(StrEnum):
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class ProjectRole(StrEnum):
    LEAD = "LEAD"
    MEMBER = "MEMBER"


class MilestoneStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"


class Project(TimestampMixin, db.Model):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProjectStatus.PLANNING.value, index=True
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    objectives: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner = relationship("User", foreign_keys=[owner_id], back_populates="owned_projects")
    creator = relationship("User", foreign_keys=[created_by_id])
    members = relationship(
        "ProjectMember",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMember.role_in_project",
    )
    milestones = relationship(
        "Milestone",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Milestone.due_date",
    )
    tasks = relationship("Task", back_populates="project")

    __table_args__ = (
        Index("ix_projects_status_target", "status", "target_date"),
    )

    @property
    def is_open(self) -> bool:
        return self.status in {
            ProjectStatus.PLANNING.value,
            ProjectStatus.ACTIVE.value,
            ProjectStatus.PAUSED.value,
        }

    @property
    def member_ids(self) -> set[int]:
        return {m.user_id for m in self.members}

    def role_of(self, user) -> str | None:
        if user is None or not getattr(user, "id", None):
            return None
        for membership in self.members:
            if membership.user_id == user.id:
                return membership.role_in_project
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Project {self.id} {self.slug!r} {self.status}>"


class ProjectMember(TimestampMixin, db.Model):
    __tablename__ = "project_members"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_in_project: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProjectRole.MEMBER.value
    )

    project = relationship("Project", back_populates="members")
    user = relationship("User", back_populates="project_memberships")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
    )

    @property
    def is_lead(self) -> bool:
        return self.role_in_project == ProjectRole.LEAD.value


class Milestone(TimestampMixin, db.Model):
    __tablename__ = "milestones"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MilestoneStatus.PENDING.value, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    project = relationship("Project", back_populates="milestones")
    completed_by = relationship("User", foreign_keys=[completed_by_id])
    tasks = relationship("Task", back_populates="milestone")

    @property
    def is_overdue(self) -> bool:
        if self.status in {MilestoneStatus.COMPLETED.value, MilestoneStatus.CANCELLED.value}:
            return False
        if self.due_date is None:
            return False
        return self.due_date < date.today()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Milestone {self.id} {self.title!r}>"
