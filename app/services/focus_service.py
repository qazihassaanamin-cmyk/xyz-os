"""Focus sessions.

The timer is server-authoritative. The browser never reports how long someone
worked; it only asks the server what the state is and tells the server to
finish a specific session. Elapsed time is always derived from stored
timestamps, so a paused period never counts and a tampered client cannot claim
credit it did not earn.

The daily credit is enforced by a partial unique index on
``(user_id, workday_date) WHERE status = 'COMPLETED'``, which makes duplicate
credit impossible even under concurrent requests.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import as_utc, utcnow
from app.models.focus import FocusSession, FocusStatus
from app.services.audit_service import record_audit
from app.utils.dates import workday_for

log = logging.getLogger(__name__)

MINIMUM_SECONDS = 20 * 60          # a session only counts once it reaches this
PAUSE_IDLE_LIMIT_SECONDS = 30 * 60  # paused longer than this becomes INTERRUPTED
RUNNING_STALE_SECONDS = 45 * 60     # no server contact for this long becomes INTERRUPTED


class FocusError(Exception):
    """A refused timer operation, with a message safe to show the member."""


def _limits() -> tuple[int, int, int]:
    from flask import current_app

    minimum = int(current_app.config.get("FOCUS_MIN_SECONDS", MINIMUM_SECONDS))
    pause_idle = int(current_app.config.get("FOCUS_PAUSE_IDLE_LIMIT_SECONDS", PAUSE_IDLE_LIMIT_SECONDS))
    running_stale = int(current_app.config.get("FOCUS_RUNNING_STALE_SECONDS", RUNNING_STALE_SECONDS))
    return minimum, pause_idle, running_stale


def current_session(user) -> FocusSession | None:
    """The member's open session, after applying the interruption policy."""
    session = (
        FocusSession.query.filter(
            FocusSession.user_id == user.id,
            FocusSession.status.in_(
                [FocusStatus.RUNNING.value, FocusStatus.PAUSED.value]
            ),
        )
        .order_by(FocusSession.id.desc())
        .first()
    )
    if session is None:
        return None
    return apply_interruption_policy(session)


def apply_interruption_policy(session: FocusSession) -> FocusSession | None:
    """Lazily interrupt an abandoned session. Returns the session or None.

    Called on every read of the timer state, so no scheduled job is needed.
    """
    _, pause_idle, running_stale = _limits()
    now = utcnow()
    changed = False
    reason = None

    if session.status == FocusStatus.PAUSED.value and session.last_resumed_at is not None:
        last = as_utc(session.last_resumed_at)
        if last is not None and (now - last).total_seconds() > pause_idle:
            reason = "Paused for longer than the allowed limit."
            changed = True
    elif session.status == FocusStatus.RUNNING.value:
        # Activity is proved by any state read; ``updated_at`` is touched by
        # every heartbeat, so a stale one means the member stopped interacting.
        last_seen = as_utc(session.updated_at or session.last_resumed_at or session.started_at)
        if last_seen is not None and (now - last_seen).total_seconds() > running_stale:
            reason = "No activity recorded for the allowed limit."
            changed = True

    if not changed:
        return session

    # Bank whatever time was genuinely accumulated before interrupting.
    if session.status == FocusStatus.RUNNING.value and session.last_resumed_at is not None:
        session.accumulated_seconds = session.elapsed_seconds(now)
    session.status = FocusStatus.INTERRUPTED.value
    session.ended_at = now
    session.duration_seconds = int(session.accumulated_seconds or 0)
    session.interrupted_reason = reason
    # Releasing the guard lets the member start again straight away.
    session.open_key = None
    db.session.commit()

    record_audit(
        "focus.interrupted",
        entity_type="focus_session",
        entity_id=session.id,
        summary=f"Focus session interrupted automatically ({reason})",
        after={"duration_seconds": session.duration_seconds},
        user_id=session.user_id,
    )
    return None


def _aware(value):
    """Backwards-compatible alias for :func:`app.models.base.as_utc`."""
    return as_utc(value)


def start_session(user, *, goal: str | None = None, task=None) -> FocusSession:
    """Start a session. Refuses when one is already open."""
    existing = current_session(user)
    if existing is not None:
        raise FocusError("You already have a session in progress. Finish or abandon it first.")

    goal_text = (goal or "").strip()[:300] or None
    if task is not None:
        from app.permissions import can_view_task

        if not can_view_task(user, task):
            raise FocusError("You cannot attach a session to that task.")

    now = utcnow()
    session = FocusSession(
        user_id=user.id,
        task_id=task.id if task else None,
        goal=goal_text,
        workday_date=workday_for(now),
        status=FocusStatus.RUNNING.value,
        started_at=now,
        last_resumed_at=now,
        accumulated_seconds=0,
        open_key=f"user:{user.id}",
    )
    db.session.add(session)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise FocusError("You already have a session in progress. Finish or abandon it first.")

    record_audit(
        "focus.started",
        entity_type="focus_session",
        entity_id=session.id,
        summary="Focus session started",
        after={"task_id": session.task_id, "goal": goal_text},
        user_id=user.id,
    )
    return session


def pause_session(session: FocusSession, *, actor) -> FocusSession:
    if session.user_id != actor.id:
        raise FocusError("That is not your session.")
    if session.status != FocusStatus.RUNNING.value:
        raise FocusError("Only a running session can be paused.")

    now = utcnow()
    session.accumulated_seconds = session.elapsed_seconds(now)
    session.last_resumed_at = now
    session.status = FocusStatus.PAUSED.value
    # Still open, so the guard stays held: a paused session is not finished.
    db.session.commit()

    record_audit(
        "focus.paused",
        entity_type="focus_session",
        entity_id=session.id,
        summary="Focus session paused",
        user_id=actor.id,
    )
    return session


def resume_session(session: FocusSession, *, actor) -> FocusSession:
    if session.user_id != actor.id:
        raise FocusError("That is not your session.")
    if session.status != FocusStatus.PAUSED.value:
        raise FocusError("Only a paused session can be resumed.")

    session.last_resumed_at = utcnow()
    session.status = FocusStatus.RUNNING.value
    db.session.commit()

    record_audit(
        "focus.resumed",
        entity_type="focus_session",
        entity_id=session.id,
        summary="Focus session resumed",
        user_id=actor.id,
    )
    return session


def heartbeat(session: FocusSession) -> FocusSession:
    """Touch the session so the interruption policy sees recent activity.

    This is the only thing the client's periodic poll does; it changes no
    duration and grants nothing.
    """
    session.updated_at = utcnow()
    db.session.commit()
    return session


class FinishOutcome:
    def __init__(self, *, completed: bool, session: FocusSession, message: str, remaining_seconds: int = 0):
        self.completed = completed
        self.session = session
        self.message = message
        self.remaining_seconds = remaining_seconds


def finish_session(session: FocusSession, *, actor) -> FinishOutcome:
    """Complete a session if it reached the minimum, else report the shortfall."""
    if session.user_id != actor.id:
        raise FocusError("That is not your session.")
    if session.status not in (FocusStatus.RUNNING.value, FocusStatus.PAUSED.value):
        raise FocusError("That session is no longer open.")

    minimum, _, _ = _limits()
    now = utcnow()
    elapsed = session.elapsed_seconds(now)

    if elapsed < minimum:
        remaining = minimum - elapsed
        # Bank the time so a resume continues rather than restarts.
        if session.status == FocusStatus.RUNNING.value:
            session.accumulated_seconds = elapsed
            session.last_resumed_at = now
        else:
            session.accumulated_seconds = elapsed
        db.session.commit()
        return FinishOutcome(
            completed=False,
            session=session,
            message=(
                f"A session only counts once it reaches {minimum // 60} minutes. "
                f"{remaining // 60} minutes {remaining % 60} seconds remaining."
            ),
            remaining_seconds=remaining,
        )

    return _complete_session(session, actor, elapsed, now)


def _complete_session(session: FocusSession, actor, elapsed: int, now) -> FinishOutcome:
    """Mark complete and take the daily credit exactly once."""
    session.status = FocusStatus.COMPLETED.value
    session.ended_at = now
    session.duration_seconds = elapsed
    session.accumulated_seconds = elapsed
    session.open_key = None

    already_credited_today = (
        FocusSession.query.filter(
            FocusSession.user_id == actor.id,
            FocusSession.workday_date == session.workday_date,
            FocusSession.status == FocusStatus.COMPLETED.value,
            FocusSession.credited.is_(True),
            FocusSession.id != session.id,
        ).first()
        is not None
    )
    session.credited = not already_credited_today
    session.credit_key = None if already_credited_today else session.credit_key_value

    try:
        db.session.commit()
    except IntegrityError:
        # The unique credit guard rejected a second credited session for the same
        # workday, or a concurrent request already claimed the credit. The work is
        # still recorded, just without a second credit.
        db.session.rollback()
        session = db.session.get(FocusSession, session.id)
        session.status = FocusStatus.COMPLETED.value
        session.ended_at = now
        session.duration_seconds = elapsed
        session.credited = False
        session.credit_key = None
        session.open_key = None
        db.session.commit()
        already_credited_today = True

    record_audit(
        "focus.completed",
        entity_type="focus_session",
        entity_id=session.id,
        summary=f"Focus session completed ({elapsed // 60} minutes)",
        after={
            "duration_seconds": elapsed,
            "credited": session.credited,
            "duplicate_of_day": already_credited_today,
        },
        user_id=actor.id,
    )

    if session.credited:
        _apply_credit(actor, session)
    else:
        _bump_session_counter(actor)

    return FinishOutcome(
        completed=True,
        session=session,
        message=(
            f"Session recorded: {elapsed // 60} minutes."
            if session.credited
            else "Session recorded. Today's daily focus credit was already earned."
        ),
    )


def _apply_credit(user, session: FocusSession) -> None:
    """XP and streak for the day's single credited session."""
    from app.models.xp import XpReason
    from app.services import xp_service
    from app.services.streak_service import record_focus_day

    try:
        xp_service.award_xp(
            user,
            XpReason.FOCUS_SESSION.value,
            note="Completed a focus session.",
            source_type="focus_session",
            source_id=session.id,
            task_id=session.task_id,
            idempotency_key=f"focus:{session.id}:completed",
        )
        session.xp_awarded_at = utcnow()
        profile = xp_service.ensure_profile(user)
        profile.focus_sessions_completed = (profile.focus_sessions_completed or 0) + 1
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Focus XP award failed for session %s", session.id)

    try:
        record_focus_day(user, session.workday_date)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Focus streak update failed for user %s", user.id)


def _bump_session_counter(user) -> None:
    """Count the extra session for statistics without granting daily credit."""
    from app.services import xp_service

    try:
        profile = xp_service.ensure_profile(user)
        profile.focus_sessions_completed = (profile.focus_sessions_completed or 0) + 1
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Focus counter update failed for user %s", user.id)


def abandon_session(session: FocusSession, *, actor) -> FocusSession:
    """Explicitly give up an open session. Recorded, never silently dropped."""
    if session.user_id != actor.id:
        raise FocusError("That is not your session.")
    if session.status not in (FocusStatus.RUNNING.value, FocusStatus.PAUSED.value):
        raise FocusError("That session is no longer open.")

    now = utcnow()
    elapsed = session.elapsed_seconds(now)
    session.status = FocusStatus.ABANDONED.value
    session.ended_at = now
    session.duration_seconds = elapsed
    session.accumulated_seconds = elapsed
    session.open_key = None
    db.session.commit()

    record_audit(
        "focus.abandoned",
        entity_type="focus_session",
        entity_id=session.id,
        summary=f"Focus session abandoned after {elapsed // 60} minutes",
        after={"duration_seconds": elapsed},
        user_id=actor.id,
    )
    return session


def state_for(user) -> dict:
    """Server state for the timer page and the polling endpoint."""
    minimum, _, _ = _limits()
    session = current_session(user)

    if session is None:
        return {
            "active": False,
            "status": None,
            "session_id": None,
            "elapsed_seconds": 0,
            "remaining_seconds": minimum,
            "minimum_seconds": minimum,
            "goal": None,
            "task_id": None,
            "can_finish": False,
            "server_time": utcnow().isoformat(),
        }

    elapsed = session.elapsed_seconds()
    return {
        "active": True,
        "status": session.status,
        "session_id": session.id,
        "elapsed_seconds": elapsed,
        "remaining_seconds": max(0, minimum - elapsed),
        "minimum_seconds": minimum,
        "goal": session.goal,
        "task_id": session.task_id,
        "can_finish": elapsed >= minimum,
        "server_time": utcnow().isoformat(),
        "is_paused": session.status == FocusStatus.PAUSED.value,
    }


def history_for(user, *, limit: int = 50, offset: int = 0) -> list[FocusSession]:
    query = (
        FocusSession.query.filter_by(user_id=user.id)
        .order_by(FocusSession.started_at.desc())
        .offset(offset)
    )
    if limit:
        query = query.limit(limit)
    return query.all()


def stats_for(user, *, days: int = 30) -> dict:
    """Focus statistics derived from stored sessions only."""
    from datetime import date

    from app.utils.dates import company_today

    today = company_today()
    since = today - timedelta(days=days)

    sessions = (
        FocusSession.query.filter(
            FocusSession.user_id == user.id,
            FocusSession.started_at.isnot(None),
            FocusSession.workday_date >= since,
        )
        .order_by(FocusSession.started_at.desc())
        .all()
    )

    completed = [s for s in sessions if s.status == FocusStatus.COMPLETED.value]
    interrupted = [s for s in sessions if s.status == FocusStatus.INTERRUPTED.value]
    abandoned = [s for s in sessions if s.status == FocusStatus.ABANDONED.value]

    total_seconds = sum(int(s.duration_seconds or 0) for s in completed)
    credited_days = {s.workday_date for s in completed if s.credited}
    completed_today = [s for s in completed if s.workday_date == today]

    # Weekly completion rate: credited working days out of working days elapsed.
    from app.services.workday_service import is_working_day_for, week_bounds

    week_start, _ = week_bounds(today)
    working_days_this_week = 0
    cursor = week_start
    while cursor <= today:
        if is_working_day_for(user, cursor):
            working_days_this_week += 1
        cursor += timedelta(days=1)
    credited_this_week = len({d for d in credited_days if d >= week_start})
    completion_rate = (
        round(100 * credited_this_week / working_days_this_week)
        if working_days_this_week
        else 0
    )

    return {
        "days": days,
        "completed_today": len(completed_today),
        "credited_today": bool(completed_today and any(s.credited for s in completed_today)),
        "completed_total": len(completed),
        "interrupted_total": len(interrupted),
        "abandoned_total": len(abandoned),
        "total_minutes": total_seconds // 60,
        "average_minutes": (total_seconds // 60 // len(completed)) if completed else 0,
        "credited_days": len(credited_days),
        "working_days_this_week": working_days_this_week,
        "credited_this_week": credited_this_week,
        "completion_rate": completion_rate,
        "sessions": sessions,
        "today": today,
        "since": since,
        "start_of_week": week_start,
    }


def daily_summary(user, *, days: int = 14) -> list[dict]:
    """Per-day focus totals for a compact history table."""
    from app.utils.dates import company_today

    today = company_today()
    since = today - timedelta(days=days)
    rows = (
        FocusSession.query.filter(
            FocusSession.user_id == user.id,
            FocusSession.workday_date >= since,
        )
        .order_by(FocusSession.workday_date.desc())
        .all()
    )

    buckets: dict = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.workday_date,
            {"date": row.workday_date, "sessions": 0, "completed": 0, "minutes": 0, "credited": False},
        )
        bucket["sessions"] += 1
        if row.status == FocusStatus.COMPLETED.value:
            bucket["completed"] += 1
            bucket["minutes"] += int(row.duration_seconds or 0) // 60
            bucket["credited"] = bucket["credited"] or bool(row.credited)

    return [buckets[key] for key in sorted(buckets.keys(), reverse=True)]
