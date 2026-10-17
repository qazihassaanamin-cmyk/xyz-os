"""Focus timer: the timer itself, its state endpoint, and its history.

The page is deliberately sparse — one column, no statistics while a session is
running, because the point of the screen is to be left alone. History and
figures live on a separate tab, and the page states plainly that a recorded
session is a completed timer, not evidence of attention.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.focus import FocusSession, FocusStatus
from app.models.task import Task, TaskStatus
from app.permissions import can_view_task, login_required
from app.services import focus_service, task_service
from app.utils.dates import company_today, humanise_seconds
from app.utils.pagination import paginate

bp = Blueprint("focus", __name__)


def _get_session_or_404(session_id: int) -> FocusSession:
    session = db.session.get(FocusSession, session_id)
    # A member may only ever address their own sessions.
    if session is None or session.user_id != current_user.id:
        abort(404)
    return session


def _selectable_tasks():
    """Open tasks the member could attach a session to."""
    return (
        task_service.visible_tasks_query(current_user, include_completed=False)
        .filter(Task.assignee_id == current_user.id)
        .order_by(Task.due_date.asc().nulls_last())
        .limit(50)
        .all()
    )


@bp.route("/")
@login_required
def index():
    """The timer. Single column, distraction-free."""
    state = focus_service.state_for(current_user)
    minimum_minutes = current_app.config.get("FOCUS_MIN_SECONDS", 1200) // 60

    return render_template(
        "focus/index.html",
        page_title="Focus timer",
        state=state,
        minimum_minutes=minimum_minutes,
        tasks=_selectable_tasks(),
        today_stats=focus_service.stats_for(current_user, days=1),
        active_tab="timer",
    )


@bp.route("/history")
@login_required
def history():
    """Session history and figures, kept off the active timer screen."""
    page = request.args.get("page")
    query = (
        FocusSession.query.filter_by(user_id=current_user.id)
        .order_by(FocusSession.started_at.desc())
    )
    pagination = paginate(query, page=page, per_page=25)

    return render_template(
        "focus/history.html",
        page_title="Focus history",
        pagination=pagination,
        sessions=pagination.items,
        stats=focus_service.stats_for(current_user, days=30),
        daily=focus_service.daily_summary(current_user, days=21),
        humanise_seconds=humanise_seconds,
        active_tab="history",
    )


@bp.route("/start", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def start():
    task = None
    task_id = request.form.get("task_id", type=int)
    if task_id:
        task = db.session.get(Task, task_id)
        if task is None or not can_view_task(current_user, task):
            abort(404)

    try:
        focus_service.start_session(
            current_user, goal=request.form.get("goal"), task=task
        )
    except focus_service.FocusError as exc:
        flash(str(exc), "error")
    else:
        flash("Session started. The timer runs on server time.", "success")
    return redirect(url_for("focus.index"))


@bp.route("/<int:session_id>/pause", methods=["POST"])
@login_required
def pause(session_id: int):
    session = _get_session_or_404(session_id)
    try:
        focus_service.pause_session(session, actor=current_user)
    except focus_service.FocusError as exc:
        flash(str(exc), "error")
    return redirect(url_for("focus.index"))


@bp.route("/<int:session_id>/resume", methods=["POST"])
@login_required
def resume(session_id: int):
    session = _get_session_or_404(session_id)
    try:
        focus_service.resume_session(session, actor=current_user)
    except focus_service.FocusError as exc:
        flash(str(exc), "error")
    return redirect(url_for("focus.index"))


@bp.route("/<int:session_id>/finish", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def finish(session_id: int):
    """Complete a session. The server decides whether it counts."""
    session = _get_session_or_404(session_id)
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    try:
        outcome = focus_service.finish_session(session, actor=current_user)
    except focus_service.FocusError as exc:
        if wants_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("focus.index"))

    if wants_json:
        return jsonify(
            {
                "ok": outcome.completed,
                "message": outcome.message,
                "remaining_seconds": outcome.remaining_seconds,
                "duration_seconds": outcome.session.duration_seconds,
                "credited": outcome.session.credited,
            }
        )

    flash(outcome.message, "success" if outcome.completed else "info")
    return redirect(url_for("focus.index"))


@bp.route("/<int:session_id>/abandon", methods=["POST"])
@login_required
def abandon(session_id: int):
    session = _get_session_or_404(session_id)
    try:
        focus_service.abandon_session(session, actor=current_user)
    except focus_service.FocusError as exc:
        flash(str(exc), "error")
    else:
        flash("Session abandoned. It is recorded in your history.", "info")
    return redirect(url_for("focus.index"))


# ---------------------------------------------------------------------------
# Server state for the browser
# ---------------------------------------------------------------------------

@bp.route("/state")
@login_required
@limiter.limit("240 per hour")
def state():
    """Authoritative timer state.

    The browser renders its countdown from this and re-syncs whenever the tab
    becomes visible again. The response never accepts a client-supplied
    duration, and reading it also proves activity so an abandoned session is
    interrupted rather than left running forever.
    """
    session = focus_service.current_session(current_user)
    if session is not None:
        focus_service.heartbeat(session)

    payload = focus_service.state_for(current_user)
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/today")
@login_required
def api_today():
    """Small summary used by the dashboard tile."""
    stats = focus_service.stats_for(current_user, days=1)
    return jsonify(
        {
            "date": company_today().isoformat(),
            "completed": stats["completed_today"],
            "credited": stats["credited_today"],
            "minimum_minutes": current_app.config.get("FOCUS_MIN_SECONDS", 1200) // 60,
        }
    )
