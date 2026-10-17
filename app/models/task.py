"""Task domain: tasks, subtasks, comments, submissions, reviews, blockers."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import StrEnum, TimestampMixin, utcnow


class TaskStatus(StrEnum):
    BACKLOG = "BACKLOG"
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    IN_REVIEW = "IN_REVIEW"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


OPEN_STATUSES = (
    TaskStatus.BACKLOG.value,
    TaskStatus.TODO.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.IN_REVIEW.value,
)


class TaskPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TaskDifficulty(StrEnum):
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"
    CRITICAL = "CRITICAL"


class SubmissionStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    SUPERSEDED = "SUPERSEDED"


class ReviewDecision(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class BlockerKind(StrEnum):
    MISSING_INFO = "MISSING_INFO"
    TECHNICAL = "TECHNICAL"
    DEPENDENCY = "DEPENDENCY"
    GUIDANCE = "GUIDANCE"
    DEADLINE = "DEADLINE"
    OTHER = "OTHER"


class BlockerStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class Task(TimestampMixin, db.Model):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    milestone_id: Mapped[int | None] = mapped_column(
        ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(220), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskPriority.NORMAL.value, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TaskStatus.TODO.value, index=True
    )
    # Set by the CEO or an authorized project lead before work begins. Members
    # can never change this on their own task.
    difficulty: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskDifficulty.MEDIUM.value
    )
    # XP the work is worth once it is genuinely accepted. Derived from the
    # difficulty at creation and editable by authorized users only.
    base_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The status the task was in when it became BLOCKED, so it can be restored.
    blocked_from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the deadline-approaching notification has been fired.
    deadline_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    overdue_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Final accepted submission that earned completion XP.
    accepted_submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True
    )

    project = relationship("Project", back_populates="tasks")
    milestone = relationship("Milestone", back_populates="tasks")
    creator = relationship("User", foreign_keys=[created_by_id], back_populates="created_tasks")
    assignee = relationship("User", foreign_keys=[assignee_id], back_populates="assigned_tasks")
    reviewer = relationship("User", foreign_keys=[reviewer_id])
    subtasks = relationship(
        "Subtask",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Subtask.position",
    )
    comments = relationship(
        "TaskComment", back_populates="task", cascade="all, delete-orphan",
        order_by="TaskComment.created_at",
    )
    submissions = relationship(
        "Submission",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Submission.created_at",
        foreign_keys="Submission.task_id",
    )
    blockers = relationship("Blocker", back_populates="task", foreign_keys="Blocker.task_id")
    attachments = relationship("Attachment", back_populates="task")
    focus_sessions = relationship("FocusSession", back_populates="task")

    __table_args__ = (
        Index("ix_tasks_assignee_status", "assignee_id", "status"),
        Index("ix_tasks_project_status", "project_id", "status"),
        Index("ix_tasks_status_due", "status", "due_date"),
    )

    # --- computed helpers -------------------------------------------------
    @property
    def status_enum(self) -> TaskStatus | None:
        return TaskStatus.coerce(self.status)

    @property
    def priority_enum(self) -> TaskPriority | None:
        return TaskPriority.coerce(self.priority)

    @property
    def difficulty_enum(self) -> TaskDifficulty | None:
        return TaskDifficulty.coerce(self.difficulty)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_overdue(self) -> bool:
        if not self.is_open or self.due_date is None:
            return False
        return self.due_date < utcnow().date()

    @property
    def days_until_due(self) -> int | None:
        if self.due_date is None:
            return None
        return (self.due_date - utcnow().date()).days

    @property
    def due_state(self) -> str:
        """Coarse bucket used by templates for emphasis."""
        if self.status == TaskStatus.COMPLETED.value:
            return "completed"
        if self.due_date is None:
            return "none"
        delta = self.days_until_due or 0
        if delta < 0:
            return "overdue"
        if delta == 0:
            return "today"
        if delta <= 2:
            return "soon"
        return "later"

    @property
    def latest_submission(self):
        if not self.submissions:
            return None
        return max(self.submissions, key=lambda s: (s.created_at, s.id))

    @property
    def pending_submission(self):
        for sub in sorted(self.submissions, key=lambda s: s.id, reverse=True):
            if sub.status == SubmissionStatus.PENDING.value:
                return sub
        return None

    @property
    def open_blockers(self):
        return [b for b in self.blockers if b.status in (BlockerStatus.OPEN.value, BlockerStatus.IN_PROGRESS.value)]

    @property
    def done_subtask_count(self) -> int:
        return sum(1 for s in self.subtasks if s.is_done)

    @property
    def checklist_progress(self) -> int:
        if not self.subtasks:
            return 0
        return round(100 * self.done_subtask_count / len(self.subtasks))

    def can_be_acknowledged_by(self, user) -> bool:
        return (
            user is not None
            and getattr(user, "id", None) == self.assignee_id
            and self.acknowledged_at is None
            and self.status in (TaskStatus.TODO.value, TaskStatus.IN_PROGRESS.value)
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task {self.id} {self.title[:40]!r} {self.status}>"


class Subtask(TimestampMixin, db.Model):
    __tablename__ = "subtasks"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    task = relationship("Task", back_populates="subtasks")


class TaskComment(TimestampMixin, db.Model):
    __tablename__ = "task_comments"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    task = relationship("Task", back_populates="comments")
    author = relationship("User")


class Submission(TimestampMixin, db.Model):
    """Append-only. A resubmission creates a new row; nothing is overwritten."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submitted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    external_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=SubmissionStatus.PENDING.value, index=True
    )
    # Snapshot of the task's XP configuration at submission time so later edits
    # to the task never silently change what an approved submission is worth.
    xp_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    difficulty_snapshot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    due_date_snapshot: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Set when completion XP for this submission has been awarded (idempotency).
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    task = relationship("Task", back_populates="submissions", foreign_keys=[task_id])
    submitted_by = relationship("User", foreign_keys=[submitted_by_id])
    reviews = relationship(
        "Review",
        back_populates="submission",
        cascade="all, delete-orphan",
        order_by="Review.decided_at",
    )
    attachments = relationship("Attachment", back_populates="submission")

    @property
    def is_pending(self) -> bool:
        return self.status == SubmissionStatus.PENDING.value

    @property
    def latest_review(self):
        if not self.reviews:
            return None
        return max(self.reviews, key=lambda r: (r.decided_at or r.created_at, r.id))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Submission {self.id} task={self.task_id} {self.status}>"


class Review(TimestampMixin, db.Model):
    """Append-only review decision."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # Whether the submission was on time relative to the task deadline when it
    # was approved; frozen so later deadline edits cannot change the record.
    was_on_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    submission = relationship("Submission", back_populates="reviews")
    reviewer = relationship("User", foreign_keys=[reviewer_id])


class Blocker(TimestampMixin, db.Model):
    __tablename__ = "blockers"

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raised_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BlockerKind.OTHER.value
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BlockerStatus.OPEN.value, index=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Who actually unblocked it when that differs from the person who raised it.
    helper_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Set once resolution XP has been granted to avoid double rewards.
    xp_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task = relationship("Task", back_populates="blockers", foreign_keys=[task_id])
    project = relationship("Project", foreign_keys=[project_id])
    raised_by = relationship("User", foreign_keys=[raised_by_id])
    assigned_to = relationship("User", foreign_keys=[assigned_to_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    helper = relationship("User", foreign_keys=[helper_id])

    __table_args__ = (
        Index("ix_blockers_status_created", "status", "created_at"),
    )

    @property
    def is_open(self) -> bool:
        return self.status in (BlockerStatus.OPEN.value, BlockerStatus.IN_PROGRESS.value)
