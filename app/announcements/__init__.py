"""Announcements."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.content import Announcement, AnnouncementRead, AnnouncementScope
from app.models.project import Project, ProjectMember, ProjectStatus
from app.models.user import User
from app.permissions import can_manage_project, is_ceo, login_required
from app.services import notification_service
from app.services.audit_service import record_audit
from app.utils.dates import company_today
from app.utils.pagination import paginate
from app.utils.validators import parse_date

bp = Blueprint("announcements", __name__)


def _visible_query(user):
    """Announcements this member may read, filtered at the query level."""
    query = Announcement.query
    if is_ceo(user):
        return query
    member_project_ids = [
        row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()
    ]
    return query.filter(
        db.or_(
            Announcement.scope == AnnouncementScope.COMPANY.value,
            Announcement.project_id.in_(member_project_ids) if member_project_ids else db.false(),
        )
    )


def _get_or_404(announcement_id: int) -> Announcement:
    announcement = db.session.get(Announcement, announcement_id)
    if announcement is None:
        abort(404)
    if not is_ceo(current_user):
        if announcement.scope == AnnouncementScope.PROJECT.value:
            project = announcement.project
            if project is None or not can_manage_project(current_user, project):
                # A member of the project may still read it.
                if project is None or current_user.id not in project.member_ids:
                    abort(404)
    return announcement


@bp.route("/")
@login_required
def index():
    """Announcements, pinned first, then newest."""
    query = _visible_query(current_user).order_by(
        Announcement.is_pinned.desc(), Announcement.created_at.desc()
    )
    pagination = paginate(query, per_page=20)

    read_ids = {
        row.announcement_id
        for row in AnnouncementRead.query.filter_by(user_id=current_user.id).all()
    }

    return render_template(
        "announcements/index.html",
        page_title="Announcements",
        pagination=pagination,
        announcements=pagination.items,
        read_ids=read_ids,
        can_publish=is_ceo(current_user),
    )


@bp.route("/<int:announcement_id>")
@login_required
def detail(announcement_id: int):
    announcement = _get_or_404(announcement_id)

    existing = AnnouncementRead.query.filter_by(
        announcement_id=announcement.id, user_id=current_user.id
    ).first()
    if existing is None:
        db.session.add(
            AnnouncementRead(announcement_id=announcement.id, user_id=current_user.id)
        )
        db.session.commit()

    return render_template(
        "announcements/detail.html",
        page_title=announcement.title,
        announcement=announcement,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per hour")
def create():
    """Publish an announcement. The CEO, or a lead for their own project."""
    if not is_ceo(current_user):
        abort(403)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        body = (request.form.get("body") or "").strip()
        if len(title) < 4:
            flash("Give the announcement a clear title.", "error")
        elif len(body) < 10:
            flash("Write the announcement before publishing it.", "error")
        else:
            scope = request.form.get("scope", AnnouncementScope.COMPANY.value)
            project = None
            project_id = request.form.get("project_id", type=int)
            if project_id:
                project = db.session.get(Project, project_id)
                if project is None:
                    abort(404)

            announcement = Announcement(
                title=title[:200],
                body=body,
                author_id=current_user.id,
                scope=scope if scope in AnnouncementScope.values() else AnnouncementScope.COMPANY.value,
                project_id=project.id if project else None,
                is_pinned=bool(request.form.get("is_pinned")),
                is_recognition=bool(request.form.get("is_recognition")),
                expires_at=None,
            )
            expires = parse_date(request.form.get("expires_at"), field="Expiry date")
            if expires is not None:
                from datetime import datetime, time

                from app.utils.dates import company_tz

                announcement.expires_at = datetime.combine(
                    expires, time(23, 59), tzinfo=company_tz()
                )
            db.session.add(announcement)
            db.session.commit()

            record_audit(
                "announcement.published",
                entity_type="announcement",
                entity_id=announcement.id,
                summary=f"Announcement “{announcement.title}” published",
                after={"scope": announcement.scope, "is_recognition": announcement.is_recognition},
                user_id=current_user.id,
            )

            recipients = _recipients_for(announcement)
            notification_service.announcement_published(
                announcement, recipients, actor=current_user
            )

            flash("Announcement published.", "success")
            return redirect(url_for("announcements.detail", announcement_id=announcement.id))

    return render_template(
        "announcements/form.html",
        page_title="New announcement",
        projects=Project.query.filter(Project.status != ProjectStatus.ARCHIVED.value)
        .order_by(Project.name.asc())
        .all(),
    )


def _recipients_for(announcement: Announcement) -> list[User]:
    if announcement.scope == AnnouncementScope.PROJECT.value and announcement.project is not None:
        return [
            membership.user
            for membership in announcement.project.members
            if membership.user is not None and membership.user.is_active
        ]
    return User.query.filter_by(is_active_flag=True).all()


@bp.route("/<int:announcement_id>/pin", methods=["POST"])
@login_required
def toggle_pin(announcement_id: int):
    if not is_ceo(current_user):
        abort(403)

    announcement = db.session.get(Announcement, announcement_id)
    if announcement is None:
        abort(404)

    announcement.is_pinned = not announcement.is_pinned
    db.session.commit()

    record_audit(
        "announcement.pin_toggled",
        entity_type="announcement",
        entity_id=announcement.id,
        summary=f"Announcement “{announcement.title}” {'pinned' if announcement.is_pinned else 'unpinned'}",
        after={"is_pinned": announcement.is_pinned},
        user_id=current_user.id,
    )
    return redirect(url_for("announcements.index"))
