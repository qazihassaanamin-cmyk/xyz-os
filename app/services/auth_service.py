"""Password authentication, lockout, session and invitation logic."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import utcnow
from app.models.login_attempt import LoginAttempt
from app.models.user import Invitation, PasswordResetToken, User, UserRole
from app.services import crypto_service
from app.services.audit_service import record_audit

log = logging.getLogger(__name__)

# --- Lockout policy ---------------------------------------------------------
# The account is temporarily locked after this many failures inside the window.
LOCKOUT_FAILURE_THRESHOLD = 5
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_DURATION_MINUTES = 15
# Hard ceiling on failures from one IP across all usernames, per window.
IP_FAILURE_CEILING = 25

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

GENERIC_LOGIN_ERROR = "Incorrect username or password."
LOCKED_ERROR = (
    "Too many failed attempts. This account is temporarily locked; "
    "try again in a few minutes."
)
INACTIVE_ERROR = "This account is not active. Contact the administrator."


class AuthError(Exception):
    """Authentication failure carrying a safe, generic message."""

    def __init__(self, message: str = GENERIC_LOGIN_ERROR, *, locked: bool = False):
        super().__init__(message)
        self.message = message
        self.locked = locked


def normalise_identifier(raw: str) -> str:
    return (raw or "").strip().lower()


def find_user_by_identifier(identifier: str) -> User | None:
    """Look up by username or email, case-insensitively."""
    value = normalise_identifier(identifier)
    if not value:
        return None
    return (
        User.query.filter(
            or_(func.lower(User.username) == value, func.lower(User.email) == value)
        ).first()
    )


def _record_attempt(identifier: str, user, success: bool, reason: str | None, ip: str | None):
    attempt = LoginAttempt(
        username_tried=normalise_identifier(identifier)[:255] or None,
        user_id=getattr(user, "id", None),
        ip_address=(ip or None) and ip[:64],
        user_agent=(current_app.request.headers.get("User-Agent") or "")[:300]
        if getattr(current_app, "request", None)
        else None,
        success=success,
        reason=reason,
    )
    db.session.add(attempt)
    try:
        db.session.commit()
    except Exception:  # pragma: no cover - never fail a login over bookkeeping
        db.session.rollback()
        log.exception("Could not record login attempt")


def is_locked(user: User | None, ip: str | None) -> bool:
    if user is not None:
        failures = LoginAttempt.recent_failures_for_user(user.id, LOCKOUT_WINDOW_MINUTES)
        if failures >= LOCKOUT_FAILURE_THRESHOLD:
            return True
    if ip:
        failures = LoginAttempt.recent_failures_for_ip(ip, LOCKOUT_WINDOW_MINUTES)
        if failures >= IP_FAILURE_CEILING:
            return True
    return False


def seconds_until_unlock(user: User | None, ip: str | None) -> int | None:
    """Approximate remaining lockout time, for display only."""
    window_start = utcnow() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    query = LoginAttempt.query.filter(
        LoginAttempt.success.is_(False), LoginAttempt.created_at >= window_start
    )
    if user is not None:
        query = query.filter(LoginAttempt.user_id == user.id)
    elif ip:
        query = query.filter(LoginAttempt.ip_address == ip)
    else:
        return None
    rows = query.order_by(LoginAttempt.created_at.asc()).limit(LOCKOUT_FAILURE_THRESHOLD).all()
    if len(rows) < LOCKOUT_FAILURE_THRESHOLD:
        return None
    unlock_at = rows[-1].created_at + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
    remaining = int((unlock_at - utcnow()).total_seconds())
    return max(0, remaining)


def authenticate(identifier: str, password: str, *, ip: str | None = None) -> User:
    """Verify credentials or raise :class:`AuthError`.

    The message returned to the caller is always the same for "no such user",
    "wrong password" and "inactive account" so the form cannot be used to
    enumerate accounts. The real reason is recorded in ``login_attempts``.
    """
    user = find_user_by_identifier(identifier)

    if is_locked(user, ip):
        _record_attempt(identifier, user, False, "locked", ip)
        raise AuthError(LOCKED_ERROR, locked=True)

    # Always run a verification so timing does not disclose account existence.
    password_ok = crypto_service.verify_password(
        user.password_hash if user else "", password or ""
    )

    if user is None:
        _record_attempt(identifier, None, False, "unknown_user", ip)
        raise AuthError()
    if not password_ok:
        _record_attempt(identifier, user, False, "bad_password", ip)
        raise AuthError()
    if not user.is_active:
        _record_attempt(identifier, user, False, "inactive", ip)
        raise AuthError(INACTIVE_ERROR)

    # Opportunistic upgrade of legacy bcrypt hashes.
    if crypto_service.password_needs_rehash(user.password_hash):
        try:
            user.password_hash = crypto_service.hash_password(password)
        except Exception:  # pragma: no cover
            log.exception("Password rehash failed for user %s", user.id)

    _record_attempt(identifier, user, True, None, ip)
    return user


def clear_lockout(user: User) -> None:
    """Remove recent failures for a user, e.g. after a successful reset."""
    cutoff = utcnow() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    LoginAttempt.query.filter(
        LoginAttempt.user_id == user.id,
        LoginAttempt.success.is_(False),
        LoginAttempt.created_at >= cutoff,
    ).delete(synchronize_session=False)
    db.session.commit()


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

def invite_ttl_minutes() -> int:
    return int(current_app.config.get("INVITE_TOKEN_TTL_MINUTES", 60 * 48))


def create_invitation(email: str, role: str, invited_by: User) -> tuple[Invitation, str]:
    """Create a single-use invitation and return it with the raw token."""
    email = normalise_identifier(email)
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if User.query.filter(func.lower(User.email) == email).first():
        raise ValueError("A member with that email already exists.")
    if role not in (UserRole.CEO.value, UserRole.MEMBER.value):
        raise ValueError("Invalid role.")

    raw_token = crypto_service.generate_token()
    invitation = Invitation(
        email=email,
        token_hash=crypto_service.hash_token(raw_token),
        role=role,
        invited_by_id=invited_by.id if invited_by else None,
        expires_at=crypto_service.default_token_expiry(invite_ttl_minutes()),
    )
    db.session.add(invitation)
    db.session.commit()
    record_audit(
        "invitation.created",
        entity_type="invitation",
        entity_id=invitation.id,
        summary=f"Invitation issued to {email} as {role}",
        after={"email": email, "role": role, "expires_at": invitation.expires_at.isoformat()},
    )
    return invitation, raw_token


def get_invitation_by_token(raw_token: str) -> Invitation | None:
    if not raw_token:
        return None
    return Invitation.query.filter_by(token_hash=crypto_service.hash_token(raw_token)).first()


def revoke_invitation(invitation: Invitation, actor: User | None) -> None:
    invitation.revoked_at = utcnow()
    db.session.commit()
    record_audit(
        "invitation.revoked",
        entity_type="invitation",
        entity_id=invitation.id,
        summary=f"Invitation for {invitation.email} revoked",
        user_id=getattr(actor, "id", None),
    )


def accept_invitation(
    raw_token: str, *, username: str, full_name: str, password: str, timezone_name: str = "Asia/Karachi"
) -> User:
    """Consume an invitation and create the member account."""
    invitation = get_invitation_by_token(raw_token)
    if invitation is None or not invitation.is_usable:
        raise AuthError("This invitation is no longer valid. Ask the CEO for a new one.")

    username = (username or "").strip()
    full_name = (full_name or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3-64 characters and may contain letters, digits, dots, dashes and underscores."
        )
    if not full_name or len(full_name) > 160:
        raise ValueError("Enter the member's full name.")
    if User.query.filter(func.lower(User.username) == username.lower()).first():
        raise ValueError("That username is already taken.")

    crypto_service.validate_password_strength(password)

    user = User(
        email=invitation.email,
        username=username,
        full_name=full_name,
        role=invitation.role,
        timezone=timezone_name or "Asia/Karachi",
        is_active_flag=True,
    )
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise ValueError("That username or email is already in use.")

    invitation.accepted_at = utcnow()
    invitation.accepted_user_id = user.id
    db.session.commit()

    _ensure_xp_profile(user)
    _grant_achievement(user, "welcome_aboard")

    record_audit(
        "invitation.accepted",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.username} accepted the invitation",
        user_id=user.id,
        after={"username": user.username, "role": user.role},
    )
    return user


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

def reset_ttl_minutes() -> int:
    return int(current_app.config.get("RESET_TOKEN_TTL_MINUTES", 60))


def create_reset_token(user: User, *, requested_ip: str | None = None) -> str:
    """Issue a reset token, invalidating any previous unused token."""
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update(
        {"used_at": utcnow()}, synchronize_session=False
    )
    raw_token = crypto_service.generate_token()
    token = PasswordResetToken(
        user_id=user.id,
        token_hash=crypto_service.hash_token(raw_token),
        expires_at=crypto_service.reset_token_expiry(reset_ttl_minutes()),
        requested_ip=(requested_ip or None) and requested_ip[:64],
    )
    db.session.add(token)
    db.session.commit()
    return raw_token


def get_reset_token(raw_token: str) -> PasswordResetToken | None:
    if not raw_token:
        return None
    return PasswordResetToken.query.filter_by(
        token_hash=crypto_service.hash_token(raw_token)
    ).first()


def complete_password_reset(raw_token: str, new_password: str) -> User:
    token = get_reset_token(raw_token)
    if token is None or not token.is_usable:
        raise AuthError("This password reset link is invalid or has expired.")

    user = token.user
    if user is None:
        raise AuthError("This password reset link is invalid or has expired.")

    crypto_service.validate_password_strength(new_password, user=user)

    user.password_hash = crypto_service.hash_password(new_password)
    user.must_change_password = False
    token.used_at = utcnow()
    db.session.commit()

    clear_lockout(user)
    _ensure_xp_profile(user)
    _grant_achievement(user, "first_lockdown")

    record_audit(
        "auth.password_reset",
        entity_type="user",
        entity_id=user.id,
        summary=f"Password reset completed for {user.username}",
        user_id=user.id,
    )
    return user


def change_own_password(user: User, current_password: str, new_password: str) -> None:
    """Change a password while the member is signed in."""
    if not user.check_password(current_password):
        raise AuthError("Your current password is not correct.")
    if user.check_password(new_password):
        raise AuthError("The new password must be different from the current one.")
    crypto_service.validate_password_strength(new_password, user=user)

    user.password_hash = crypto_service.hash_password(new_password)
    user.must_change_password = False
    db.session.commit()

    _ensure_xp_profile(user)
    _grant_achievement(user, "first_lockdown")

    record_audit(
        "auth.password_changed",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.username} changed their password",
        user_id=user.id,
    )


# ---------------------------------------------------------------------------
# Small internal helpers that touch Phase G models without importing them at
# module import time (keeps the import graph acyclic).
# ---------------------------------------------------------------------------

def _ensure_xp_profile(user: User) -> None:
    try:
        from app.services.xp_service import ensure_profile

        ensure_profile(user)
    except Exception:  # pragma: no cover - gamification must never block auth
        db.session.rollback()
        log.exception("Could not ensure XP profile for user %s", getattr(user, "id", None))


def _grant_achievement(user: User, code: str) -> None:
    try:
        from app.services.achievement_service import grant

        grant(user, code)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Could not grant achievement %s to user %s", code, getattr(user, "id", None))
