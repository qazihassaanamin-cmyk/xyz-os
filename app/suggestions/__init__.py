"""Ideas and suggestions.

The composer asks for the problem before the idea, because a suggestion without
a stated problem cannot be evaluated fairly. Anonymous mode is off by default
and, when enabled, the page says plainly what it does and does not protect.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.project import Project, ProjectMember, ProjectStatus
from app.models.suggestion import (
    SUGGESTION_STATUS_LABELS,
    Suggestion,
    SuggestionCategory,
    SuggestionStatus,
)
from app.permissions import is_ceo, login_required
from app.services import settings_service, suggestion_service
from app.utils.dates import humanise_delta
from app.utils.pagination import paginate

bp = Blueprint("suggestions", __name__)


def _get_suggestion_or_404(suggestion_id: int) -> Suggestion:
    suggestion = db.session.get(Suggestion, suggestion_id)
    if suggestion is None:
        abort(404)
    return suggestion


@bp.route("/")
@login_required
def index():
    """The board. Editorial and readable, ordered by recency by default."""
    status = request.args.get("status", "")
    category = request.args.get("category", "")
    project_id = request.args.get("project", type=int)
    sort = request.args.get("sort", "recent")
    mine = request.args.get("mine") == "1"

    query = suggestion_service.visible_query(
        current_user,
        status=status.upper() if status else None,
        category=category.upper() if category else None,
        project_id=project_id,
        mine=mine,
        sort=sort,
    )
    pagination = paginate(query, per_page=20)
    items = pagination.items

    voted_ids = set()
    if items:
        from app.models.suggestion import SuggestionVote

        voted_ids = {
            row.suggestion_id
            for row in SuggestionVote.query.filter(
                SuggestionVote.user_id == current_user.id,
                SuggestionVote.suggestion_id.in_([row.id for row in items]),
            ).all()
        }

    return render_template(
        "suggestions/index.html",
        page_title="Ideas and suggestions",
        pagination=pagination,
        suggestions=items,
        voted_ids=voted_ids,
        status=status,
        category=category,
        project_id=project_id,
        sort=sort,
        mine=mine,
        stats=suggestion_service.stats(),
        categories=SuggestionCategory.values(),
        statuses=SuggestionStatus.values(),
        status_labels=SUGGESTION_STATUS_LABELS,
        projects=_project_choices(),
        anonymous_enabled=settings_service.get_bool("anonymous_suggestions_enabled", False),
        voting_enabled=settings_service.get_bool("suggestion_votes_enabled", True),
        humanise_delta=humanise_delta,
    )


def _project_choices():
    query = Project.query.filter(Project.status != ProjectStatus.ARCHIVED.value)
    if not is_ceo(current_user):
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).filter(
            ProjectMember.user_id == current_user.id
        )
    return query.order_by(Project.name.asc()).all()


@bp.route("/new", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per hour")
def create():
    anonymous_enabled = settings_service.get_bool("anonymous_suggestions_enabled", False)

    if request.method == "POST":
        project = None
        project_id = request.form.get("project_id", type=int)
        if project_id:
            project = db.session.get(Project, project_id)
            from app.permissions import can_view_project

            if project is None or not can_view_project(current_user, project):
                abort(404)

        try:
            suggestion = suggestion_service.create_suggestion(
                actor=current_user,
                title=request.form.get("title", ""),
                body=request.form.get("body", ""),
                category=request.form.get("category", SuggestionCategory.PROCESS.value),
                project=project,
                is_anonymous=bool(request.form.get("is_anonymous")) and anonymous_enabled,
            )
        except suggestion_service.SuggestionError as exc:
            flash(str(exc), "error")
        else:
            flash("Suggestion recorded.", "success")
            return redirect(url_for("suggestions.detail", suggestion_id=suggestion.id))

    return render_template(
        "suggestions/form.html",
        page_title="New suggestion",
        categories=SuggestionCategory.values(),
        projects=_project_choices(),
        anonymous_enabled=anonymous_enabled,
    )


@bp.route("/<int:suggestion_id>")
@login_required
def detail(suggestion_id: int):
    suggestion = _get_suggestion_or_404(suggestion_id)

    from app.models.suggestion import SuggestionVote

    voted = (
        SuggestionVote.query.filter_by(
            suggestion_id=suggestion.id, user_id=current_user.id
        ).first()
        is not None
    )

    can_decide = suggestion_service.can_decide(current_user, suggestion)

    return render_template(
        "suggestions/detail.html",
        page_title=suggestion.title,
        suggestion=suggestion,
        voted=voted,
        can_decide=can_decide,
        statuses=SuggestionStatus.values(),
        status_labels=SUGGESTION_STATUS_LABELS,
        voting_enabled=settings_service.get_bool("suggestion_votes_enabled", True),
        projects=_project_choices(),
        humanise_delta=humanise_delta,
    )


@bp.route("/<int:suggestion_id>/vote", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def vote(suggestion_id: int):
    suggestion = _get_suggestion_or_404(suggestion_id)
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    try:
        added = suggestion_service.toggle_vote(suggestion, actor=current_user)
    except suggestion_service.SuggestionError as exc:
        if wants_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("suggestions.detail", suggestion_id=suggestion.id))

    if wants_json:
        return jsonify(
            {
                "ok": True,
                "voted": added,
                "votes": suggestion.vote_count or 0,
            }
        )
    flash("Support recorded." if added else "Support withdrawn.", "success")
    return redirect(url_for("suggestions.detail", suggestion_id=suggestion.id))


@bp.route("/<int:suggestion_id>/status", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def change_status(suggestion_id: int):
    """Move a suggestion through its lifecycle. CEO or the project lead."""
    suggestion = _get_suggestion_or_404(suggestion_id)
    if not suggestion_service.can_decide(current_user, suggestion):
        abort(403)

    try:
        suggestion_service.change_status(
            suggestion,
            actor=current_user,
            new_status=request.form.get("status", ""),
            note=request.form.get("note"),
        )
    except suggestion_service.SuggestionError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Suggestion marked as {suggestion.status_label}.", "success")
    return redirect(url_for("suggestions.detail", suggestion_id=suggestion.id))


@bp.route("/<int:suggestion_id>/convert", methods=["POST"])
@login_required
def convert(suggestion_id: int):
    """Turn a planned suggestion into a real task."""
    suggestion = _get_suggestion_or_404(suggestion_id)
    if not suggestion_service.can_decide(current_user, suggestion):
        abort(403)

    project = db.session.get(Project, request.form.get("project_id", type=int))
    if project is None:
        flash("Choose the project this work belongs to.", "error")
        return redirect(url_for("suggestions.detail", suggestion_id=suggestion.id))

    try:
        task = suggestion_service.convert_to_task(
            suggestion,
            actor=current_user,
            project=project,
            title=request.form.get("title"),
        )
    except suggestion_service.SuggestionError as exc:
        flash(str(exc), "error")
        return redirect(url_for("suggestions.detail", suggestion_id=suggestion.id))

    flash("Suggestion converted into a task.", "success")
    return redirect(url_for("tasks.detail", task_id=task.id))


@bp.route("/reviews")
@login_required
def reviews():
    """Review queue for suggestions awaiting a decision."""
    if not is_ceo(current_user):
        abort(403)

    query = Suggestion.query.filter(
        Suggestion.status.in_(
            [SuggestionStatus.OPEN.value, SuggestionStatus.UNDER_REVIEW.value]
        )
    ).order_by(Suggestion.created_at.asc())
    pagination = paginate(query, per_page=20)

    return render_template(
        "suggestions/reviews.html",
        page_title="Suggestion reviews",
        pagination=pagination,
        suggestions=pagination.items,
        status_labels=SUGGESTION_STATUS_LABELS,
        projects=_project_choices(),
        humanise_delta=humanise_delta,
    )
