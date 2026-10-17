"""Task lifecycle: the state machine, assignment, submissions and reviews.

The allowed transitions are declared once, here, and every status change in the
application goes through :func:`transition`. That keeps the rules testable and
prevents a route from inventing its own path between statuses.
"""

from __future__ import annotations

import logging

from app.extensions import db
from app.models.base import utcnow
from app.models.project import Milestone, MilestoneStatus, Project, ProjectMember, ProjectRole
from app.models.task import (
    Blocker,
    BlockerStatus,
    Review,
    ReviewDecision,
    Submission,
    SubmissionStatus,
    Subtask,
    Task,
    TaskComment,
    TaskDifficulty,
    TaskPriority,
    TaskStatus,
)
from app.services.audit_service import record_audit
from app.services.notification_service import (
    blocker_raised,
    blocker_resolved,
    comment_added,
    review_decision,
    submission_received,
    task_assigned,
)
from app.utils.dates import company_today
from app.utils.validators import ValidationError, clean_text, optional_text, require_text

log = logging.getLogger(__name__)


class TaskError(Exception):
    """A refused operation, with a message safe to show the member."""


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------

# from_status -> {to_status: description}
ALLOWED_TRANSITIONS: dict[str, dict[str, str]] = {
    TaskStatus.BACKLOG.value: {
        TaskStatus.TODO.value: "Move into the working queue",
        TaskStatus.CANCELLED.value: "Cancel the task",
    },
    TaskStatus.TODO.value: {
        TaskStatus.BACKLOG.value: "Return to the backlog",
        TaskStatus.IN_PROGRESS.value: "Start work",
        TaskStatus.BLOCKED.value: "Mark as blocked",
        TaskStatus.CANCELLED.value: "Cancel the task",
    },
    TaskStatus.IN_PROGRESS.value: {
        TaskStatus.TODO.value: "Return to the queue",
        TaskStatus.BLOCKED.value: "Mark as blocked",
        TaskStatus.IN_REVIEW.value: "Submit for review",
        TaskStatus.CANCELLED.value: "Cancel the task",
    },
    TaskStatus.BLOCKED.value: {
        TaskStatus.TODO.value: "Unblock and return to the queue",
        TaskStatus.IN_PROGRESS.value: "Unblock and continue",
        TaskStatus.CANCELLED.value: "Cancel the task",
    },
    TaskStatus.IN_REVIEW.value: {
        TaskStatus.IN_PROGRESS.value: "Request changes",
        TaskStatus.COMPLETED.value: "Approve the work",
        TaskStatus.BLOCKED.value: "Mark as blocked",
        TaskStatus.CANCELLED.value: "Cancel the task",
    },
    TaskStatus.COMPLETED.value: {
        # Reopening is CEO-only and always logged; it exists so an incorrect
        # approval can be corrected without editing history.
        TaskStatus.IN_PROGRESS.value: "Reopen the task",
    },
    TaskStatus.CANCELLED.value: {
        TaskStatus.TODO.value: "Restore the task",
    },
}

# Statuses a member may set on their own task without a submission.
MEMBER_DIRECT_TARGETS = {
    TaskStatus.TODO.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.BACKLOG.value,
}

CEO_ONLY_TARGETS = {TaskStatus.CANCELLED.value}


def available_transitions(task: Task, user) -> dict[str, str]:
    """Transitions this user may currently make on this task."""
    from app.permissions import can_edit_task, can_review_submission, is_ceo

    options = dict(ALLOWED_TRANSITIONS.get(task.status, {}))
    if not options:
        return {}

    is_assignee = getattr(user, "id", None) == task.assignee_id
    ceo = is_ceo(user)
    lead = can_edit_task(user, task)

    result: dict[str, str] = {}
    for target, label in options.items():
        if target in CEO_ONLY_TARGETS and not ceo:
            continue
        if target == TaskStatus.IN_REVIEW.value:
            # Reaching review always goes through a submission, never a raw
            # status change, so the reviewer has something to assess.
            continue
        if target == TaskStatus.COMPLETED.value:
            pending = task.pending_submission
            if pending is None or not can_review_submission(user, pending):
                continue
        if target == TaskStatus.IN_PROGRESS.value and task.status == TaskStatus.IN_REVIEW.value:
            # Only a reviewer may send work back.
            pending = task.pending_submission
            if pending is None or not can_review_submission(user, pending):
                continue
        if target in MEMBER_DIRECT_TARGETS and not (is_assignee or lead or ceo):
            continue
        if not (is_assignee or lead or ceo):
            continue
        result[target] = label
    return result


def transition(
    task: Task,
    target_status: str,
    *,
    actor,
    reason: str | None = None,
    note: str | None = None,
) -> Task:
    """Move a task to ``target_status`` or raise :class:`TaskError`.

    Every legal transition writes an audit row in the same transaction, so the
    history of a task can always be reconstructed.
    """
    from app.permissions import can_change_task_status, can_edit_task, can_review_submission, is_ceo

    target_status = (target_status or "").strip().upper()
    if target_status not in TaskStatus.values():
        raise TaskError("That is not a valid task status.")
    if target_status == task.status:
        raise TaskError("The task is already in that status.")

    allowed = ALLOWED_TRANSITIONS.get(task.status, {})
    if target_status not in allowed:
        raise TaskError(
            f"A task cannot move from {_label(task.status)} to {_label(target_status)}."
        )

    if not can_change_task_status(actor, task, target_status):
        raise TaskError("You do not have permission to change this task.")

    is_assignee = getattr(actor, "id", None) == task.assignee_id
    lead_or_ceo = can_edit_task(actor, task)
    ceo = is_ceo(actor)

    if target_status in CEO_ONLY_TARGETS and not ceo:
        raise TaskError("Only the CEO can cancel a task.")
    if target_status == TaskStatus.COMPLETED.value and not ceo and not lead_or_ceo:
        # Approval also requires being the reviewer of the pending submission.
        pending = task.pending_submission
        if pending is None or not can_review_submission(actor, pending):
            raise TaskError("Only the assigned reviewer can approve this work.")
    if target_status == TaskStatus.IN_REVIEW.value:
        raise TaskError("Submit the work rather than setting the status directly.")
    if target_status == TaskStatus.BLOCKED.value and not task.open_blockers:
        raise TaskError("Record a blocker before marking the task as blocked.")
    if task.status == TaskStatus.BLOCKED.value and target_status != TaskStatus.CANCELLED.value:
        open_rows = [b for b in task.blockers if b.is_open]
        if open_rows:
            raise TaskError("Resolve the open blocker before moving this task forward.")
    if target_status == TaskStatus.IN_PROGRESS.value and task.status == TaskStatus.IN_REVIEW.value:
        pending = task.pending_submission
        if pending is None or not can_review_submission(actor, pending):
            raise TaskError("Only the assigned reviewer can send work back.")

    previous = task.status
    task.status = target_status

    if target_status == TaskStatus.BLOCKED.value:
        task.blocked_from_status = previous
    elif previous == TaskStatus.BLOCKED.value:
        task.blocked_from_status = None

    if target_status == TaskStatus.COMPLETED.value:
        task.completed_at = utcnow()
    elif previous == TaskStatus.COMPLETED.value:
        task.completed_at = None
        task.accepted_submission_id = None

    if target_status == TaskStatus.CANCELLED.value:
        task.cancelled_reason = note or reason

    db.session.commit()

    record_audit(
        "task.status_changed",
        entity_type="task",
        entity_id=task.id,
        summary=f"“{task.title}” moved from {_label(previous)} to {_label(target_status)}",
        before={"status": previous},
        after={"status": target_status, "reason": reason or note},
        user_id=getattr(actor, "id", None),
    )
    _after_transition(task, previous, target_status, actor)
    return task


def _after_transition(task: Task, previous: str, target: str, actor) -> None:
    """Side effects that must never break the transition itself."""
    from app.services import xp_service

    try:
        if target == TaskStatus.COMPLETED.value:
            profile = xp_service.ensure_profile(task.assignee) if task.assignee else None
            if profile is not None:
                profile.tasks_completed = (profile.tasks_completed or 0) + 1
                db.session.commit()
        if previous == TaskStatus.COMPLETED.value and target != TaskStatus.COMPLETED.value:
            # Reopening removes the counter but never the XP record: a reversal
            # is issued explicitly by the CEO so the ledger stays honest.
            profile = xp_service.ensure_profile(task.assignee) if task.assignee else None
            if profile is not None and profile.tasks_completed > 0:
                profile.tasks_completed -= 1
                db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Task counter update failed for task %s", task.id)


def _label(status: str) -> str:
    return (status or "").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Creation and editing
# ---------------------------------------------------------------------------

def create_task(
    *,
    actor,
    title: str,
    project: Project | None,
    assignee=None,
    reviewer=None,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    priority: str = TaskPriority.NORMAL.value,
    difficulty: str = TaskDifficulty.MEDIUM.value,
    due_date=None,
    start_date=None,
    milestone: Milestone | None = None,
) -> Task:
    """Create a task. Only the CEO or a project lead reaches this function."""
    from app.permissions import can_assign_task

    if not can_assign_task(actor, project):
        raise TaskError("You do not have permission to create tasks here.")

    title = require_text(title, field="Task title", min_length=3, max_length=220)
    priority = _coerce(priority, TaskPriority, "priority")
    difficulty = _coerce(difficulty, TaskDifficulty, "difficulty")

    if project is not None and assignee is not None and assignee.id not in project.member_ids:
        raise TaskError(f"{assignee.display_name} is not a member of {project.name}.")
    if milestone is not None and project is not None and milestone.project_id != project.id:
        raise TaskError("That milestone belongs to a different project.")
    if assignee is not None and reviewer is not None and assignee.id == reviewer.id:
        raise TaskError("A task cannot be reviewed by its own assignee.")
    if due_date is not None and start_date is not None and due_date < start_date:
        raise TaskError("The deadline cannot be before the start date.")

    from app.services.xp_service import DIFFICULTY_MULTIPLIERS, rule_for
    from app.models.xp import XpReason

    rule = rule_for(XpReason.TASK_COMPLETED.value)
    base_points = rule.points if rule else 100

    task = Task(
        title=title,
        description=optional_text(description),
        acceptance_criteria=optional_text(acceptance_criteria),
        project_id=project.id if project else None,
        milestone_id=milestone.id if milestone else None,
        created_by_id=actor.id,
        assignee_id=getattr(assignee, "id", None),
        reviewer_id=getattr(reviewer, "id", None),
        priority=priority,
        difficulty=difficulty,
        base_xp=base_points,
        start_date=start_date,
        due_date=due_date,
        status=TaskStatus.TODO.value if assignee is not None else TaskStatus.BACKLOG.value,
    )
    db.session.add(task)
    db.session.commit()

    record_audit(
        "task.created",
        entity_type="task",
        entity_id=task.id,
        summary=f"Task “{task.title}” created"
        + (f" in {project.name}" if project else ""),
        after={
            "title": task.title,
            "assignee_id": task.assignee_id,
            "difficulty": task.difficulty,
            "priority": task.priority,
            "due_date": task.due_date.isoformat() if task.due_date else None,
        },
        user_id=actor.id,
    )
    if task.assignee is not None:
        task_assigned(task, actor=actor)
    return task


def update_task(task: Task, *, actor, **fields) -> Task:
    """Update a task, enforcing who may change what."""
    from app.permissions import can_edit_task, can_edit_task_details, can_set_task_difficulty

    if not can_edit_task_details(actor, task):
        raise TaskError("You do not have permission to edit this task.")

    full_edit = can_edit_task(actor, task)
    before: dict = {}
    after: dict = {}

    def record(key, new_value):
        old_value = getattr(task, key, None)
        if old_value != new_value:
            before[key] = _serialise(old_value)
            after[key] = _serialise(new_value)
            setattr(task, key, new_value)

    if "title" in fields and fields["title"] is not None:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can rename a task.")
        record("title", require_text(fields["title"], field="Task title", min_length=3, max_length=220))

    if "description" in fields:
        record("description", optional_text(fields["description"]))
    if "progress_note" in fields:
        record("progress_note", optional_text(fields["progress_note"], max_length=4000))
    if "acceptance_criteria" in fields:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can change acceptance criteria.")
        record("acceptance_criteria", optional_text(fields["acceptance_criteria"]))

    if "priority" in fields and fields["priority"] is not None:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can change priority.")
        record("priority", _coerce(fields["priority"], TaskPriority, "priority"))

    if "difficulty" in fields and fields["difficulty"] is not None:
        if not can_set_task_difficulty(actor, task):
            raise TaskError("Only the CEO or the project lead can set task difficulty.")
        record("difficulty", _coerce(fields["difficulty"], TaskDifficulty, "difficulty"))

    if "base_xp" in fields and fields["base_xp"] is not None:
        if not can_set_task_difficulty(actor, task):
            raise TaskError("Only the CEO or the project lead can set the XP reward.")
        try:
            value = int(fields["base_xp"])
        except (TypeError, ValueError):
            raise TaskError("The XP reward must be a whole number.")
        if not 0 <= value <= 1000:
            raise TaskError("The XP reward must be between 0 and 1000.")
        record("base_xp", value)

    if "due_date" in fields:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can change the deadline.")
        record("due_date", fields["due_date"])
    if "start_date" in fields:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can change the start date.")
        record("start_date", fields["start_date"])

    if "assignee_id" in fields and fields["assignee_id"] is not None:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can reassign a task.")
        from app.models.user import User

        assignee = db.session.get(User, int(fields["assignee_id"]))
        if assignee is None or not assignee.is_active:
            raise TaskError("Choose an active member.")
        if task.project is not None and assignee.id not in task.project.member_ids:
            raise TaskError(f"{assignee.display_name} is not a member of {task.project.name}.")
        if task.reviewer_id == assignee.id:
            raise TaskError("A task cannot be assigned to its own reviewer.")
        record("assignee_id", assignee.id)
        if task.status == TaskStatus.BACKLOG.value:
            record("status", TaskStatus.TODO.value)

    if "reviewer_id" in fields:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can change the reviewer.")
        from app.models.user import User

        raw = fields["reviewer_id"]
        if raw in (None, "", "0"):
            record("reviewer_id", None)
        else:
            reviewer = db.session.get(User, int(raw))
            if reviewer is None or not reviewer.is_active:
                raise TaskError("Choose an active member as reviewer.")
            if reviewer.id == task.assignee_id:
                raise TaskError("A task cannot be reviewed by its own assignee.")
            record("reviewer_id", reviewer.id)

    if "milestone_id" in fields:
        if not full_edit:
            raise TaskError("Only the task owner or the CEO can change the milestone.")
        raw = fields["milestone_id"]
        if raw in (None, "", "0"):
            record("milestone_id", None)
        else:
            milestone = db.session.get(Milestone, int(raw))
            if milestone is None or (task.project_id and milestone.project_id != task.project_id):
                raise TaskError("That milestone does not belong to this project.")
            record("milestone_id", milestone.id)

    if not after:
        return task

    if task.due_date is not None and task.start_date is not None and task.due_date < task.start_date:
        raise TaskError("The deadline cannot be before the start date.")

    # A new deadline re-arms the deadline notifications.
    if "due_date" in after:
        task.deadline_notified_at = None
        task.overdue_notified_at = None

    db.session.commit()

    record_audit(
        "task.updated",
        entity_type="task",
        entity_id=task.id,
        summary=f"Task “{task.title}” updated",
        before=before,
        after=after,
        user_id=actor.id,
    )
    if "assignee_id" in after:
        task_assigned(task, actor=actor)
    return task


def _serialise(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _coerce(value: str, enum_cls, field: str) -> str:
    member = enum_cls.coerce(value)
    if member is None:
        raise TaskError(f"Choose a valid {field}.")
    return member.value


def acknowledge_task(task: Task, *, actor) -> Task:
    """The assignee confirms they have read and accepted the task."""
    from app.permissions import can_acknowledge_task

    if not can_acknowledge_task(actor, task):
        raise TaskError("This task cannot be acknowledged by you right now.")
    task.acknowledged_at = utcnow()
    db.session.commit()

    record_audit(
        "task.acknowledged",
        entity_type="task",
        entity_id=task.id,
        summary=f"{actor.display_name} acknowledged “{task.title}”",
        user_id=actor.id,
    )
    return task


def delete_task(task: Task, *, actor) -> None:
    """Cancel rather than destroy, so history and XP records survive."""
    from app.permissions import can_delete_task

    if not can_delete_task(actor, task):
        raise TaskError("You do not have permission to cancel this task.")
    if task.status == TaskStatus.CANCELLED.value:
        raise TaskError("That task is already cancelled.")

    previous = task.status
    task.status = TaskStatus.CANCELLED.value
    db.session.commit()

    record_audit(
        "task.cancelled",
        entity_type="task",
        entity_id=task.id,
        summary=f"Task “{task.title}” cancelled",
        before={"status": previous},
        after={"status": TaskStatus.CANCELLED.value},
        user_id=actor.id,
    )


# ---------------------------------------------------------------------------
# Subtasks and comments
# ---------------------------------------------------------------------------

def add_subtask(task: Task, *, title: str, actor) -> Subtask:
    from app.permissions import can_edit_task_details

    if not can_edit_task_details(actor, task):
        raise TaskError("You do not have permission to change this checklist.")
    title = require_text(title, field="Checklist item", min_length=1, max_length=240)
    position = (max((s.position for s in task.subtasks), default=-1) + 1) if task.subtasks else 0
    row = Subtask(task_id=task.id, title=title, position=position)
    db.session.add(row)
    db.session.commit()
    record_audit(
        "task.subtask_added",
        entity_type="task",
        entity_id=task.id,
        summary=f"Checklist item added to “{task.title}”",
        after={"title": title},
        user_id=actor.id,
    )
    return row


def toggle_subtask(subtask: Subtask, *, actor, is_done: bool | None = None) -> Subtask:
    from app.permissions import can_edit_task_details

    task = subtask.task
    if not can_edit_task_details(actor, task):
        raise TaskError("You do not have permission to change this checklist.")
    target = (not subtask.is_done) if is_done is None else bool(is_done)
    subtask.is_done = target
    subtask.done_at = utcnow() if target else None
    subtask.done_by_id = actor.id if target else None
    db.session.commit()
    return subtask


def remove_subtask(subtask: Subtask, *, actor) -> None:
    from app.permissions import can_edit_task_details

    task = subtask.task
    if not can_edit_task_details(actor, task):
        raise TaskError("You do not have permission to change this checklist.")
    db.session.delete(subtask)
    db.session.commit()


def add_comment(task: Task, *, actor, body: str) -> TaskComment:
    from app.permissions import can_view_task

    if not can_view_task(actor, task):
        raise TaskError("You do not have access to this task.")
    body = require_text(body, field="Comment", min_length=1, max_length=6000)
    comment = TaskComment(task_id=task.id, author_id=actor.id, body=body)
    db.session.add(comment)
    db.session.commit()

    record_audit(
        "task.comment_added",
        entity_type="task",
        entity_id=task.id,
        summary=f"Comment added to “{task.title}”",
        user_id=actor.id,
    )
    comment_added(task, comment, actor=actor)
    return comment


# ---------------------------------------------------------------------------
# Submissions and reviews
# ---------------------------------------------------------------------------

def submit_work(
    task: Task,
    *,
    actor,
    summary: str,
    external_link: str | None = None,
    attachment_count: int = 0,
    quality_claimed: bool = False,
) -> Submission:
    """Create a submission and move the task into review.

    Reaching ``IN_REVIEW`` is only possible through this function, so a reviewer
    always has something concrete to assess.
    """
    from app.permissions import can_submit_work

    if not can_submit_work(actor, task):
        raise TaskError("You cannot submit work for this task.")

    summary = require_text(summary, field="Submission summary", min_length=10, max_length=6000)
    external_link = (external_link or "").strip() or None
    if external_link:
        from app.utils.validators import parse_external_link

        external_link = parse_external_link(external_link)

    if attachment_count == 0 and not external_link:
        raise TaskError("Attach at least one file or provide a link to the work.")

    previous_submissions = (
        Submission.query.filter_by(task_id=task.id).order_by(Submission.id.desc()).all()
    )
    # A superseded submission is closed out so only one is ever pending.
    for row in previous_submissions:
        if row.status == SubmissionStatus.PENDING.value:
            row.status = SubmissionStatus.SUPERSED.value
    for row in previous_submissions:
        if row.status == SubmissionStatus.CHANGES_REQUESTED.value:
            row.status = SubmissionStatus.SUPERSED.value

    submission = Submission(
        task_id=task.id,
        submitted_by_id=actor.id,
        summary=summary,
        external_link=external_link,
        status=SubmissionStatus.PENDING.value,
        xp_snapshot=task.base_xp or 0,
        difficulty_snapshot=task.difficulty,
        due_date_snapshot=task.due_date,
        revision_number=len(previous_submissions) + 1,
    )
    db.session.add(submission)

    previous_status = task.status
    task.status = TaskStatus.IN_REVIEW.value
    db.session.commit()

    record_audit(
        "task.submitted",
        entity_type="task",
        entity_id=task.id,
        summary=f"Work submitted for “{task.title}” (revision {submission.revision_number})",
        before={"status": previous_status},
        after={"status": TaskStatus.IN_REVIEW.value, "submission_id": submission.id},
        user_id=actor.id,
    )
    submission_received(submission, actor=actor)
    return submission


def review_submission(
    submission: Submission,
    *,
    actor,
    decision: str,
    feedback: str | None = None,
    quality_bonus: bool = False,
) -> Review:
    """Approve or return a submission. Feedback is mandatory when returning work."""
    from app.permissions import can_review_submission

    if not can_review_submission(actor, submission):
        raise TaskError("You cannot review this submission.")

    decision = (decision or "").strip().upper()
    if decision not in ReviewDecision.values():
        raise TaskError("Choose approve or request changes.")

    feedback = clean_text(feedback, max_length=6000) or None
    if decision == ReviewDecision.CHANGES_REQUESTED.value and not feedback:
        raise TaskError("Explain what needs to change before returning the work.")
    if decision == ReviewDecision.APPROVED.value and feedback is None:
        feedback = None

    task = submission.task
    if task is None:
        raise TaskError("That submission is no longer attached to a task.")

    on_time = _was_on_time(task)

    review = Review(
        submission_id=submission.id,
        reviewer_id=actor.id,
        decision=decision,
        feedback=feedback,
        decided_at=utcnow(),
        was_on_time=on_time,
    )
    db.session.add(review)

    submission.status = (
        SubmissionStatus.APPROVED.value
        if decision == ReviewDecision.APPROVED.value
        else SubmissionStatus.CHANGES_REQUESTED.value
    )

    previous_status = task.status
    if decision == ReviewDecision.APPROVED.value:
        task.status = TaskStatus.COMPLETED.value
        task.completed_at = utcnow()
        task.accepted_submission_id = submission.id
    else:
        task.status = TaskStatus.IN_PROGRESS.value
        task.completed_at = None

    db.session.commit()

    record_audit(
        "review.decided",
        entity_type="submission",
        entity_id=submission.id,
        summary=(
            f"Submission for “{task.title}” "
            + ("approved" if decision == ReviewDecision.APPROVED.value else "returned for changes")
        ),
        before={"task_status": previous_status},
        after={"decision": decision, "task_status": task.status, "on_time": on_time},
        user_id=actor.id,
    )

    _apply_review_effects(
        task,
        submission,
        actor=actor,
        decision=decision,
        on_time=on_time,
        quality_bonus=quality_bonus,
    )
    review_decision(submission, decision, feedback, actor=actor)
    return review


def _was_on_time(task: Task) -> bool:
    if task.due_date is None:
        return False
    return company_today() <= task.due_date


def _apply_review_effects(task, submission, *, actor, decision, on_time, quality_bonus) -> None:
    """XP and counters. Each step is isolated so a failure cannot lose the review."""
    from app.models.xp import XpReason
    from app.services import xp_service

    assignee = task.assignee
    if assignee is None:
        return

    profile = xp_service.ensure_profile(assignee)

    try:
        profile.reviews_approved = (profile.reviews_approved or 0) + 1
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Review counter update failed")

    # The reviewer earns a small amount for doing the review itself.
    try:
        xp_service.award_xp(
            actor,
            XpReason.REVIEW_QUALITY_BONUS.value,
            amount=15,
            note=f"Completed a review of “{task.title}”.",
            source_type="submission",
            source_id=submission.id,
            task_id=task.id,
            project_id=task.project_id,
            idempotency_key=f"review:{submission.id}:{actor.id}",
        )
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Reviewer XP award failed")

    if decision != ReviewDecision.APPROVED.value:
        return

    try:
        xp_service.award_task_completion(
            assignee,
            task=task,
            submission=submission,
            base_xp=submission.xp_snapshot or task.base_xp or 0,
            difficulty=submission.difficulty_snapshot or task.difficulty,
            on_time=on_time,
            quality_bonus=bool(quality_bonus),
            awarded_by=actor,
        )
        submission.xp_awarded_at = utcnow()

        if on_time:
            profile.tasks_on_time = (profile.tasks_on_time or 0) + 1
        profile.tasks_completed = (profile.tasks_completed or 0) + 1
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Completion XP award failed for task %s", task.id)

    try:
        from app.services.achievement_service import evaluate_all

        evaluate_all(assignee)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Achievement evaluation failed after review")


def submission_history(task: Task) -> list[Submission]:
    return (
        Submission.query.filter_by(task_id=task.id)
        .order_by(Submission.created_at.desc(), Submission.id.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------

def raise_blocker(
    *,
    actor,
    description: str,
    kind: str,
    task: Task | None = None,
    project: Project | None = None,
    assigned_to=None,
) -> Blocker:
    from app.permissions import can_raise_blocker

    if task is not None and not can_raise_blocker(actor, task):
        raise TaskError("You cannot raise a blocker on this task.")
    description = require_text(description, field="Blocker description", min_length=10, max_length=4000)

    from app.models.task import BlockerKind

    kind_value = BlockerKind.coerce(kind)
    if kind_value is None:
        raise TaskError("Choose a blocker type.")

    if task is None and project is None:
        raise TaskError("A blocker must relate to a task or a project.")

    blocker = Blocker(
        task_id=task.id if task else None,
        project_id=project.id if project else (task.project_id if task else None),
        raised_by_id=actor.id,
        assigned_to_id=getattr(assigned_to, "id", None),
        kind=kind_value.value,
        description=description,
        status=BlockerStatus.OPEN.value,
    )
    db.session.add(blocker)

    if task is not None and task.status != TaskStatus.BLOCKED.value:
        task.blocked_from_status = task.status
        task.status = TaskStatus.BLOCKED.value

    db.session.commit()

    record_audit(
        "blocker.raised",
        entity_type="blocker",
        entity_id=blocker.id,
        summary=f"Blocker raised by {actor.display_name}"
        + (f" on “{task.title}”" if task else ""),
        after={"kind": blocker.kind, "task_id": blocker.task_id, "project_id": blocker.project_id},
        user_id=actor.id,
    )
    blocker_raised(blocker, actor=actor)
    return blocker


def resolve_blocker(
    blocker: Blocker,
    *,
    actor,
    resolution_note: str,
    helper=None,
) -> Blocker:
    from app.permissions import can_help_with_blocker, can_resolve_blocker

    if not (can_resolve_blocker(actor, blocker) or can_help_with_blocker(actor, blocker)):
        raise TaskError("You cannot resolve this blocker.")

    resolution_note = require_text(
        resolution_note, field="Resolution note", min_length=5, max_length=4000
    )

    blocker.status = BlockerStatus.RESOLVED.value
    blocker.resolution_note = resolution_note
    blocker.resolved_at = utcnow()
    blocker.resolved_by_id = actor.id
    if helper is not None:
        blocker.helper_id = helper.id

    task = blocker.task
    if task is not None and task.status == TaskStatus.BLOCKED.value:
        still_open = [
            b for b in task.blockers if b.id != blocker.id and b.is_open
        ]
        if not still_open:
            restore = task.blocked_from_status or TaskStatus.IN_PROGRESS.value
            if restore == TaskStatus.BLOCKED.value:
                restore = TaskStatus.IN_PROGRESS.value
            task.status = restore
            task.blocked_from_status = None

    db.session.commit()

    record_audit(
        "blocker.resolved",
        entity_type="blocker",
        entity_id=blocker.id,
        summary=f"Blocker resolved by {actor.display_name}",
        before={"status": BlockerStatus.OPEN.value},
        after={"status": BlockerStatus.RESOLVED.value, "note": resolution_note},
        user_id=actor.id,
    )

    _award_blocker_xp(blocker, actor, helper)
    blocker_resolved(blocker, actor=actor)
    return blocker


def _award_blocker_xp(blocker: Blocker, actor, helper) -> None:
    from app.models.xp import XpReason
    from app.services import xp_service

    if blocker.xp_awarded_at is not None:
        return

    try:
        xp_service.award_xp(
            actor,
            XpReason.BLOCKER_RESOLVED.value,
            note="Resolved a blocker that was stopping work.",
            source_type="blocker",
            source_id=blocker.id,
            task_id=blocker.task_id,
            project_id=blocker.project_id,
            idempotency_key=f"blocker:{blocker.id}:resolved",
        )
        # A second member who helped also earns credit, once.
        if helper is not None and helper.id != actor.id:
            xp_service.award_xp(
                helper,
                XpReason.BLOCKER_HELPED.value,
                note="Helped unblock a team member.",
                source_type="blocker",
                source_id=blocker.id,
                task_id=blocker.task_id,
                project_id=blocker.project_id,
                idempotency_key=f"blocker:{blocker.id}:helped:{helper.id}",
                awarded_by=actor,
            )
        blocker.xp_awarded_at = utcnow()

        profile = xp_service.ensure_profile(actor)
        profile.blockers_resolved = (profile.blockers_resolved or 0) + 1
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Blocker XP award failed for blocker %s", blocker.id)


def assign_helper(blocker: Blocker, *, helper, actor) -> Blocker:
    from app.permissions import can_resolve_blocker

    if not can_resolve_blocker(actor, blocker):
        raise TaskError("You cannot assign help for this blocker.")
    blocker.assigned_to_id = getattr(helper, "id", None)
    blocker.status = BlockerStatus.IN_PROGRESS.value
    db.session.commit()

    record_audit(
        "blocker.helper_assigned",
        entity_type="blocker",
        entity_id=blocker.id,
        summary=f"Help requested from {getattr(helper, 'display_name', 'a member')}",
        after={"assigned_to_id": blocker.assigned_to_id},
        user_id=actor.id,
    )
    if helper is not None:
        try:
            from app.services.notification_service import notify

            notify(
                helper,
                "BLOCKER_RAISED",
                title="You were asked to help with a blocker",
                body=blocker.description[:300],
                link_endpoint="tasks.blockers",
                subject_type="blocker",
                subject_id=blocker.id,
                dedupe_key=f"blocker:{blocker.id}:helper:{helper.id}",
                actor=actor,
            )
        except Exception:  # pragma: no cover
            log.exception("Helper notification failed")
    return blocker


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

def complete_milestone(milestone: Milestone, *, actor) -> Milestone:
    from app.permissions import can_manage_project

    if not can_manage_project(actor, milestone.project):
        raise TaskError("Only the CEO or the project lead can complete a milestone.")
    if milestone.status == MilestoneStatus.COMPLETED.value:
        raise TaskError("That milestone is already complete.")

    milestone.status = MilestoneStatus.COMPLETED.value
    milestone.completed_at = utcnow()
    milestone.completed_by_id = actor.id
    db.session.commit()

    record_audit(
        "milestone.completed",
        entity_type="milestone",
        entity_id=milestone.id,
        summary=f"Milestone “{milestone.title}” completed",
        after={"project_id": milestone.project_id},
        user_id=actor.id,
    )

    try:
        from app.models.xp import XpReason
        from app.services import xp_service

        xp_service.award_xp(
            actor,
            XpReason.MILESTONE_COMPLETED.value,
            note=f"Completed the milestone “{milestone.title}”.",
            source_type="milestone",
            source_id=milestone.id,
            project_id=milestone.project_id,
            idempotency_key=f"milestone:{milestone.id}:completed",
        )
        profile = xp_service.ensure_profile(actor)
        profile.milestones_completed = (profile.milestones_completed or 0) + 1
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Milestone XP award failed")

    return milestone


def create_milestone(
    *, actor, project: Project, title: str, description: str | None, due_date=None
) -> Milestone:
    from app.permissions import can_manage_project

    if not can_manage_project(actor, project):
        raise TaskError("Only the CEO or the project lead can add milestones.")
    title = require_text(title, field="Milestone title", min_length=3, max_length=200)
    milestone = Milestone(
        project_id=project.id,
        title=title,
        description=optional_text(description),
        due_date=due_date,
        status=MilestoneStatus.PENDING.value,
    )
    db.session.add(milestone)
    db.session.commit()

    record_audit(
        "milestone.created",
        entity_type="milestone",
        entity_id=milestone.id,
        summary=f"Milestone “{title}” added to {project.name}",
        user_id=actor.id,
    )
    return milestone


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def visible_tasks_query(user, *, include_completed: bool = True):
    """Tasks this member may see, filtered at the query level."""
    from app.models.user import User
    from app.permissions import is_ceo

    query = Task.query
    if not is_ceo(user):
        member_project_ids = [
            row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()
        ]
        query = query.filter(
            db.or_(
                Task.assignee_id == user.id,
                Task.reviewer_id == user.id,
                Task.created_by_id == user.id,
                Task.project_id.in_(member_project_ids) if member_project_ids else db.false(),
            )
        )
    if not include_completed:
        query = query.filter(Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]))
    _ = User
    return query


def overdue_tasks_query(user=None):
    query = Task.query.filter(
        Task.status.notin_(
            [TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value, TaskStatus.BACKLOG.value]
        ),
        Task.due_date.isnot(None),
        Task.due_date < company_today(),
    )
    if user is not None:
        query = query.filter(Task.assignee_id == user.id)
    return query.order_by(Task.due_date.asc())


def due_soon_tasks_query(days: int = 3, user=None):
    today = company_today()
    from datetime import timedelta

    query = Task.query.filter(
        Task.status.notin_([TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]),
        Task.due_date.isnot(None),
        Task.due_date >= today,
        Task.due_date <= today + timedelta(days=days),
    )
    if user is not None:
        query = query.filter(Task.assignee_id == user.id)
    return query.order_by(Task.due_date.asc())


def ensure_project_lead(project: Project, user) -> None:
    """Delegate project leadership. CEO-only; used by the project admin route."""
    if user.id in project.member_ids:
        for membership in project.members:
            if membership.user_id == user.id:
                membership.role_in_project = ProjectRole.LEAD.value
    else:
        db.session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role_in_project=ProjectRole.LEAD.value,
            )
        )
    db.session.commit()
