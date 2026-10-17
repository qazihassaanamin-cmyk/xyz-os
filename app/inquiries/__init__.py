"""Inquiries: the CEO asks a founder a question, the founder replies.

Every member can read the board. Only the CEO can raise an inquiry. Only the
founder it is addressed to can reply. The status is a column on the row and a
history table records each change, so a resolution can always be reconstructed
with its reason and the person who made it.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.suggestion import Inquiry, InquiryStatus
from app.models.user import User
from app.permissions import ceo_required, is_ceo, login_required
from app.services import inquiry_service

bp = Blueprint("inquiries", __name__)


@bp.route("/")
@login_required
def index():
    """The inquiry board. Visible to every member; raising is CEO-only."""
    inquiries = inquiry_service.for_member(current_user)
    return render_template(
        "inquiries/index.html",
        page_title="Inquiries",
        inquiries=inquiries,
        stats=inquiry_service.stats(),
        is_ceo=is_ceo(current_user),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@ceo_required
@limiter.limit("20 per hour", methods=["POST"])
def new():
    """Raise an inquiry. Addressed to one founder or company-wide."""
    if request.method == "POST":
        subject = request.form.get("subject", "")
        body = request.form.get("body", "")
        addressed_id = request.form.get("addressed_to", type=int)
        addressed_to = db.session.get(User, addressed_id) if addressed_id else None

        try:
            inquiry = inquiry_service.raise_inquiry(
                actor=current_user,
                subject=subject,
                body=body,
                addressed_to=addressed_to,
            )
        except inquiry_service.InquiryError as exc:
            flash(str(exc), "error")
            return redirect(url_for("inquiries.new"))

        flash("Inquiry raised. The founder has been asked for a written reply.", "success")
        return redirect(url_for("inquiries.detail", inquiry_id=inquiry.id))

    founders = User.query.filter_by(role="CEO").order_by(User.full_name).all()
    members = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()
    return render_template(
        "inquiries/new.html",
        page_title="Raise an inquiry",
        founders=founders,
        members=members,
    )


@bp.route("/<int:inquiry_id>")
@login_required
def detail(inquiry_id: int):
    """A single inquiry and its replies."""
    inquiry = db.session.get(Inquiry, inquiry_id)
    if inquiry is None:
        abort(404)

    can_view = (
        inquiry.raised_by_id == current_user.id
        or inquiry.addressed_to_id == current_user.id
        or inquiry.addressed_to_id is None
    )
    if not can_view:
        abort(403)

    can_reply = (
        inquiry.addressed_to_id == current_user.id
        or inquiry.addressed_to_id is None
        or inquiry.raised_by_id == current_user.id
    )
    can_close = inquiry.raised_by_id == current_user.id

    return render_template(
        "inquiries/detail.html",
        page_title=inquiry.subject,
        inquiry=inquiry,
        can_reply=can_reply,
        can_close=can_close,
        is_ceo=is_ceo(current_user),
    )


@bp.route("/<int:inquiry_id>/reply", methods=["POST"])
@login_required
@limiter.limit("30 per hour", methods=["POST"])
def reply(inquiry_id: int):
    """A founder replies to an inquiry addressed to them."""
    inquiry = db.session.get(Inquiry, inquiry_id)
    if inquiry is None:
        abort(404)

    can_reply = (
        inquiry.addressed_to_id == current_user.id
        or inquiry.addressed_to_id is None
        or inquiry.raised_by_id == current_user.id
    )
    if not can_reply:
        abort(403)

    try:
        inquiry_service.reply(inquiry, actor=current_user, body=request.form.get("body", ""))
    except inquiry_service.InquiryError as exc:
        flash(str(exc), "error")
    else:
        flash("Your reply has been recorded.", "success")

    return redirect(url_for("inquiries.detail", inquiry_id=inquiry.id))


@bp.route("/<int:inquiry_id>/close", methods=["POST"])
@login_required
@ceo_required
@limiter.limit("20 per hour", methods=["POST"])
def close(inquiry_id: int):
    """Close an inquiry. CEO only."""
    inquiry = db.session.get(Inquiry, inquiry_id)
    if inquiry is None:
        abort(404)

    try:
        inquiry_service.close(inquiry, actor=current_user, note=request.form.get("note", ""))
    except inquiry_service.InquiryError as exc:
        flash(str(exc), "error")
    else:
        flash("Inquiry closed.", "success")

    return redirect(url_for("inquiries.detail", inquiry_id=inquiry.id))


@bp.route("/<int:inquiry_id>/resolve", methods=["POST"])
@login_required
@ceo_required
@limiter.limit("20 per hour", methods=["POST"])
def resolve(inquiry_id: int):
    """Mark an inquiry resolved. CEO only — a founder's reply is not the end."""
    inquiry = db.session.get(Inquiry, inquiry_id)
    if inquiry is None:
        abort(404)

    try:
        inquiry_service.resolve(
            inquiry,
            actor=current_user,
            note=request.form.get("resolution_note", ""),
        )
    except inquiry_service.InquiryError as exc:
        flash(str(exc), "error")
    else:
        flash("Inquiry marked resolved.", "success")

    return redirect(url_for("inquiries.detail", inquiry_id=inquiry.id))