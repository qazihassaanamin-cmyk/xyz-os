"""Knowledge base: internal documents, decisions and reference material.

Content is written in markdown-lite and rendered through a filter that escapes
first and permits only a small set of tags, so a document can never inject
markup or script. Publishing an entry is a verified, XP-eligible contribution.
"""

from __future__ import annotations

import logging

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.content import KnowledgeCategory, KnowledgeEntry, KnowledgeStatus
from app.models.project import Project, ProjectMember, ProjectStatus
from app.models.user import User
from app.permissions import can_view_project, is_ceo, login_required
from app.services.audit_service import record_audit
from app.utils import markdown_lite
from app.utils.pagination import paginate
from app.utils.validators import parse_slug, require_text

log = logging.getLogger(__name__)

bp = Blueprint("knowledge", __name__)


def _visible_query(user):
    """Published entries for everyone; drafts only for the author and the CEO."""
    query = KnowledgeEntry.query
    if is_ceo(user):
        return query
    member_project_ids = [
        row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()
    ]
    return query.filter(
        db.and_(
            db.or_(
                KnowledgeEntry.status == KnowledgeStatus.PUBLISHED.value,
                KnowledgeEntry.author_id == user.id,
            ),
            db.or_(
                KnowledgeEntry.project_id.is_(None),
                KnowledgeEntry.project_id.in_(member_project_ids) if member_project_ids else db.false(),
            ),
        )
    )


def _get_or_404(entry_id: int) -> KnowledgeEntry:
    entry = db.session.get(KnowledgeEntry, entry_id)
    if entry is None:
        abort(404)
    if entry.status == KnowledgeStatus.DRAFT.value:
        if not (is_ceo(current_user) or entry.author_id == current_user.id):
            abort(404)
    if entry.project_id is not None and not can_view_project(current_user, entry.project):
        abort(404)
    return entry


@bp.route("/")
@login_required
def index():
    """Browse and search. Search covers title and body."""
    query_string = (request.args.get("q") or "").strip()
    category = request.args.get("category", "")
    project_id = request.args.get("project", type=int)
    status = request.args.get("status", "")

    query = _visible_query(current_user)

    if query_string:
        like = f"%{query_string}%"
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(like),
                KnowledgeEntry.body.ilike(like),
                KnowledgeEntry.summary.ilike(like),
            )
        )
    if category:
        query = query.filter(KnowledgeEntry.category == category.upper())
    if project_id:
        query = query.filter(KnowledgeEntry.project_id == project_id)
    if status:
        query = query.filter(KnowledgeEntry.status == status.upper())

    query = query.order_by(
        KnowledgeEntry.is_decision_record.desc(),
        KnowledgeEntry.updated_at.desc(),
    )
    pagination = paginate(query, per_page=20)

    return render_template(
        "knowledge/index.html",
        page_title="Knowledge",
        pagination=pagination,
        entries=pagination.items,
        query_string=query_string,
        category=category,
        project_id=project_id,
        status=status,
        categories=KnowledgeCategory.values(),
        projects=_project_choices(),
        excerpt=markdown_lite.excerpt,
    )


def _project_choices():
    query = Project.query.filter(Project.status != ProjectStatus.ARCHIVED.value)
    if not is_ceo(current_user):
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).filter(
            ProjectMember.user_id == current_user.id
        )
    return query.order_by(Project.name.asc()).all()


@bp.route("/<int:entry_id>")
@login_required
def detail(entry_id: int):
    entry = _get_or_404(entry_id)
    entry.view_count = (entry.view_count or 0) + 1
    db.session.commit()

    return render_template(
        "knowledge/detail.html",
        page_title=entry.title,
        entry=entry,
        body_html=markdown_lite.render(entry.body),
        can_edit=is_ceo(current_user) or entry.author_id == current_user.id,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per hour")
def create():
    if request.method == "POST":
        try:
            entry = _save_entry(None)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(
                "Entry published." if entry.is_published else "Draft saved.",
                "success",
            )
            return redirect(url_for("knowledge.detail", entry_id=entry.id))

    return render_template(
        "knowledge/form.html",
        page_title="New knowledge entry",
        entry=None,
        categories=KnowledgeCategory.values(),
        projects=_project_choices(),
    )


@bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit(entry_id: int):
    entry = _get_or_404(entry_id)
    if not (is_ceo(current_user) or entry.author_id == current_user.id):
        abort(403)

    if request.method == "POST":
        try:
            entry = _save_entry(entry)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Entry updated.", "success")
            return redirect(url_for("knowledge.detail", entry_id=entry.id))

    return render_template(
        "knowledge/form.html",
        page_title=f"Edit: {entry.title}",
        entry=entry,
        categories=KnowledgeCategory.values(),
        projects=_project_choices(),
    )


def _save_entry(existing: KnowledgeEntry | None) -> KnowledgeEntry:
    title = require_text(request.form.get("title", ""), field="Title", min_length=4, max_length=200)
    body = require_text(request.form.get("body", ""), field="Body", min_length=20, max_length=40000)
    summary = (request.form.get("summary") or "").strip()[:400] or None
    category = request.form.get("category", KnowledgeCategory.TECHNICAL.value)
    if category.upper() not in KnowledgeCategory.values():
        category = KnowledgeCategory.TECHNICAL.value

    project = None
    project_id = request.form.get("project_id", type=int)
    if project_id:
        project = db.session.get(Project, project_id)
        if project is None or not can_view_project(current_user, project):
            abort(404)

    publish = request.form.get("publish") == "1"

    if existing is None:
        slug = _unique_slug(parse_slug(title))
        entry = KnowledgeEntry(
            title=title,
            slug=slug,
            summary=summary,
            body=body,
            category=category.upper(),
            author_id=current_user.id,
            project_id=project.id if project else None,
            status=KnowledgeStatus.DRAFT.value,
            is_decision_record=bool(request.form.get("is_decision_record")),
        )
        db.session.add(entry)
        db.session.commit()
    else:
        before = {
            "title": existing.title,
            "status": existing.status,
            "category": existing.category,
        }
        entry = existing
        entry.title = title
        entry.summary = summary
        entry.body = body
        entry.category = category.upper()
        entry.project_id = project.id if project else None
        entry.is_decision_record = bool(request.form.get("is_decision_record"))
        db.session.commit()

        record_audit(
            "knowledge.updated",
            entity_type="knowledge_entry",
            entity_id=entry.id,
            summary=f"Knowledge entry “{entry.title}” updated",
            before=before,
            after={"title": entry.title, "status": entry.status},
            user_id=current_user.id,
        )

    if publish and entry.status != KnowledgeStatus.PUBLISHED.value:
        _publish(entry)

    return entry


def _unique_slug(base: str) -> str:
    slug = base
    suffix = 2
    while KnowledgeEntry.query.filter_by(slug=slug).first() is not None:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _publish(entry: KnowledgeEntry) -> None:
    """Publish and award the documentation credit once."""
    from app.models.base import utcnow
    from app.models.xp import XpReason
    from app.services import xp_service

    entry.status = KnowledgeStatus.PUBLISHED.value
    entry.published_at = utcnow()
    db.session.commit()

    record_audit(
        "knowledge.published",
        entity_type="knowledge_entry",
        entity_id=entry.id,
        summary=f"Knowledge entry “{entry.title}” published",
        after={"category": entry.category, "project_id": entry.project_id},
        user_id=current_user.id,
    )

    if entry.xp_awarded_at is not None:
        return

    try:
        xp_service.award_xp(
            current_user,
            XpReason.KNOWLEDGE_DOCUMENTED.value,
            note=f"Published “{entry.title}” to the knowledge base.",
            source_type="knowledge_entry",
            source_id=entry.id,
            project_id=entry.project_id,
            idempotency_key=f"knowledge:{entry.id}:published",
        )
        entry.xp_awarded_at = utcnow()
        profile = xp_service.ensure_profile(current_user)
        profile.knowledge_entries = (profile.knowledge_entries or 0) + 1
        db.session.commit()

        from app.services.achievement_service import evaluate_all

        evaluate_all(current_user)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Knowledge XP award failed for entry %s", entry.id)


@bp.route("/<int:entry_id>/archive", methods=["POST"])
@login_required
def archive(entry_id: int):
    entry = _get_or_404(entry_id)
    if not (is_ceo(current_user) or entry.author_id == current_user.id):
        abort(403)

    entry.status = KnowledgeStatus.ARCHIVED.value
    db.session.commit()

    record_audit(
        "knowledge.archived",
        entity_type="knowledge_entry",
        entity_id=entry.id,
        summary=f"Knowledge entry “{entry.title}” archived",
        user_id=current_user.id,
    )
    flash("Entry archived. It remains searchable by the CEO.", "info")
    return redirect(url_for("knowledge.index"))
