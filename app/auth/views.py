"""Authentication routes: login, logout, invitations and password resets.

Every route here is server-authoritative. The login form is rate limited by IP
and by username, failed attempts are recorded, and the error message never
reveals whether an account exists.
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.extensions import db, limiter
from app.models.base import utcnow
from app.services import auth_service
from app.services.audit_service import record_audit
from app.services.crypto_service import PasswordError
from app.services.mail_service import send_invitation_email, send_reset_email
from app.auth.forms import (
    AcceptInviteForm,
    ChangePasswordForm,
    ForgotPasswordForm,
    LoginForm,
    ResetPasswordForm,
)

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Only redirect to same-origin absolute paths."""
    if not target:
        return url_for("dashboard.index")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("dashboard.index")
    return target


def _client_ip() -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(
    "10 per minute;60 per hour",
    methods=["POST"],
    error_message="Too many sign-in attempts from this address. Please wait and try again.",
)
def login():
    """Sign-in screen. The first thing an unauthenticated visitor sees."""
    if current_user.is_authenticated and current_user.is_active:
        return redirect(_safe_next(request.args.get("next")))

    form = LoginForm()
    next_target = request.args.get("next") or request.form.get("next")

    if form.validate_on_submit():
        identifier = form.identifier.data
        password = form.password.data
        try:
            user = auth_service.authenticate(identifier, password, ip=_client_ip())
        except auth_service.AuthError as exc:
            # One generic message for unknown user, wrong password and inactive
            # account. The precise reason is in the login_attempts table.
            flash(exc.message, "error")
            record_audit(
                "auth.login_failed",
                entity_type="user",
                summary=f"Failed sign-in for {auth_service.normalise_identifier(identifier)[:80]}",
                after={"reason": "locked" if exc.locked else "invalid_credentials"},
                user_id=None,
                commit=True,
            )
            return render_template(
                "auth/login.html",
                form=form,
                next_target=next_target,
                page_title="Sign in",
            ), 401

        _start_session(user, remember=bool(form.remember.data))
        record_audit(
            "auth.login_succeeded",
            entity_type="user",
            entity_id=user.id,
            summary=f"{user.username} signed in",
            user_id=user.id,
        )
        flash(f"Welcome back, {user.display_name.split()[0]}.", "success")
        return redirect(_safe_next(next_target))

    return render_template(
        "auth/login.html",
        form=form,
        next_target=next_target,
        page_title="Sign in",
    )


def _start_session(user, *, remember: bool) -> None:
    """Establish a fresh session, defeating session fixation."""
    session.clear()
    login_user(user, remember=remember, fresh=True)
    session.permanent = True
    session["_session_started"] = utcnow().isoformat()
    user.last_login_at = utcnow()
    db.session.commit()
    from app.services.achievement_service import grant as grant_achievement

    grant_achievement(user, "early_bird")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """Sign out. POST only, so a stray link cannot end a session."""
    actor_id = current_user.id
    username = current_user.username
    logout_user()
    session.clear()
    record_audit(
        "auth.logout",
        entity_type="user",
        entity_id=actor_id,
        summary=f"{username} signed out",
        user_id=actor_id,
    )
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit(
    "5 per 15 minutes;20 per hour",
    methods=["POST"],
    error_message="Too many reset requests. Please wait before trying again.",
)
def forgot_password():
    """Request a reset link. Always responds identically for known/unknown email."""
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = auth_service.normalise_identifier(form.email.data)
        user = auth_service.find_user_by_identifier(email)

        # Uniform response regardless of whether the address exists.
        generic_message = (
            "If that email address belongs to a member, a reset link has been sent. "
            "Check your inbox and spam folder."
        )

        if user is not None and user.is_active:
            raw_token = auth_service.create_reset_token(user, requested_ip=_client_ip())
            link = url_for("auth.reset_password", token=raw_token, _external=True)
            send_reset_email(user, link)
            record_audit(
                "auth.reset_requested",
                entity_type="user",
                entity_id=user.id,
                summary=f"Password reset requested for {user.username}",
                user_id=user.id,
            )
        else:
            # Record the attempt so abuse is visible without leaking to the client.
            record_audit(
                "auth.reset_requested_unknown",
                summary=f"Password reset requested for unknown address {email[:80]}",
                user_id=None,
            )

        flash(generic_message, "info")
        return redirect(url_for("auth.login"))

    return render_template(
        "auth/forgot_password.html", form=form, page_title="Reset your password"
    )


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token: str):
    """Set a new password using a single-use token."""
    if current_user.is_authenticated:
        logout_user()
        session.clear()

    reset_token = auth_service.get_reset_token(token)
    if reset_token is None or not reset_token.is_usable:
        flash("This password reset link is invalid or has expired.", "error")
        return render_template(
            "auth/reset_invalid.html", page_title="Reset link expired"
        ), 410

    form = ResetPasswordForm()
    if form.validate_on_submit():
        try:
            user = auth_service.complete_password_reset(token, form.password.data)
        except PasswordError as exc:
            form.password.errors.append(str(exc))
        except auth_service.AuthError as exc:
            flash(exc.message, "error")
            return render_template(
                "auth/reset_invalid.html", page_title="Reset link expired"
            ), 410
        else:
            flash("Your password has been updated. Sign in with your new password.", "success")
            return redirect(url_for("auth.login"))

    return render_template(
        "auth/reset_password.html",
        form=form,
        token=token,
        member_email=reset_token.user.email if reset_token.user else None,
        page_title="Set a new password",
    )


@bp.route("/invite/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def accept_invite(token: str):
    """Single-use invitation acceptance. Creates the member account."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    invitation = auth_service.get_invitation_by_token(token)
    if invitation is None or not invitation.is_usable:
        flash("This invitation is no longer valid. Ask the CEO for a new one.", "error")
        return render_template(
            "auth/invite_invalid.html", page_title="Invitation not valid"
        ), 410

    form = AcceptInviteForm()
    if form.validate_on_submit():
        try:
            user = auth_service.accept_invitation(
                token,
                username=form.username.data,
                full_name=form.full_name.data,
                password=form.password.data,
                timezone_name=form.timezone.data or "Asia/Karachi",
            )
        except PasswordError as exc:
            form.password.errors.append(str(exc))
        except ValueError as exc:
            flash(str(exc), "error")
        except auth_service.AuthError as exc:
            flash(exc.message, "error")
            return render_template(
                "auth/invite_invalid.html", page_title="Invitation not valid"
            ), 410
        else:
            _start_session(user, remember=False)
            flash(
                "Your account is ready. Add your first check-in to get started.",
                "success",
            )
            return redirect(url_for("dashboard.index"))

    return render_template(
        "auth/accept_invite.html",
        form=form,
        invitation=invitation,
        token=token,
        page_title="Create your account",
    )


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def change_password():
    """Change the signed-in member's own password."""
    form = ChangePasswordForm()
    if form.validate_on_submit():
        try:
            auth_service.change_own_password(
                current_user, form.current_password.data, form.password.data
            )
        except PasswordError as exc:
            form.password.errors.append(str(exc))
        except auth_service.AuthError as exc:
            form.current_password.errors.append(exc.message)
        else:
            flash("Your password has been updated.", "success")
            return redirect(url_for("dashboard.index"))

    return render_template(
        "auth/change_password.html",
        form=form,
        page_title="Change password",
        forced=bool(current_user.must_change_password),
    )


@bp.route("/health")
def health():
    """Unauthenticated liveness probe. Reveals nothing about application data."""
    try:
        db.session.execute(db.text("SELECT 1"))
        database_ok = True
    except Exception:  # pragma: no cover
        database_ok = False
    status = 200 if database_ok else 503
    return jsonify({"status": "ok" if database_ok else "degraded", "database": database_ok}), status
