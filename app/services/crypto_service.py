"""Password hashing and constant-time verification.

Argon2id is the primary algorithm. bcrypt is accepted for verification so
existing hashes keep working after a deployment; on a successful bcrypt check
the hash is upgraded in place. Verification always runs a dummy hash when the
account does not exist or is inactive so the response time does not reveal
whether an account is present.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from flask import current_app

try:  # bcrypt is optional at runtime but pinned in requirements.txt
    import bcrypt
except ImportError:  # pragma: no cover
    bcrypt = None

log = logging.getLogger(__name__)

# Single module-level hasher so the tuned parameters are shared everywhere.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,   # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

# A deliberately cheap hasher used only when the operator opts in, via the
# ``FAST_PASSWORD_HASHING`` flag. It is never suitable for a real deployment:
# it is fast enough to brute force offline. Tests set it so the suite does not
# spend its time waiting on Argon2.
_fast_hasher = PasswordHasher(
    time_cost=1,
    memory_cost=8192,    # 8 MiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def _active_hasher() -> PasswordHasher:
    """The hasher used for new hashes.

    ``FAST_PASSWORD_HASHING`` is a deliberate opt-in for the test suite. It is
    never suitable for a real deployment: it is fast enough to brute force
    offline, so it must never be left on outside of tests.
    """
    try:
        if current_app.config.get("FAST_PASSWORD_HASHING", False):
            return _fast_hasher
    except RuntimeError:
        pass
    return _hasher

MIN_PASSWORD_LENGTH = 12

# Hashed once at import time; used for constant-time behaviour on unknown users.
_SLOW_DUMMY_HASH = _hasher.hash("tavryc-dummy-password-never-used-for-login")
_FAST_DUMMY_HASH = _fast_hasher.hash("tavryc-dummy-password-never-used-for-login")

COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "123456", "12345678",
    "123456789", "1234567890", "qwerty", "qwerty123", "letmein", "welcome",
    "welcome1", "admin", "administrator", "iloveyou", "monkey", "dragon",
    "football", "baseball", "abc123", "xyz123", "tavryc123", "changeme",
    "change-me", "secret", "master", "sunshine", "princess", "trustno1",
}


class PasswordError(ValueError):
    """Raised with a user-facing message when a password is unacceptable."""


def hash_password(raw_password: str) -> str:
    return _active_hasher().hash(raw_password)


def _hasher_for(stored_hash: str) -> PasswordHasher:
    """Pick the hasher that matches the parameters encoded in ``stored_hash``.

    Argon2 encodes its parameters in the hash string itself
    (``$argon2id$v=19$m=<mem>,t=<time>,p=<par>``), so a hash produced by the
    cheap test hasher verifies against the cheap hasher and a production hash
    against the production hasher. This keeps verification fast in the test
    suite without weakening anything that reaches a real deployment.
    """
    if not stored_hash.startswith("$argon2"):
        return _hasher
    try:
        params = stored_hash.split("$", 3)[-1]
        mem = time_cost = par = None
        for piece in params.split(","):
            key, _, value = piece.partition("=")
            if key == "m":
                mem = int(value)
            elif key == "t":
                time_cost = int(value)
            elif key == "p":
                par = int(value)
        if mem is not None and time_cost is not None and par is not None:
            if mem == _fast_hasher.memory_cost and time_cost == _fast_hasher.time_cost and par == _fast_hasher.parallelism:
                return _fast_hasher
    except (ValueError, IndexError):
        pass
    return _hasher


def verify_password(stored_hash: str, raw_password: str) -> bool:
    """Constant-time-ish verification across Argon2 and bcrypt hashes."""
    if not stored_hash or not raw_password:
        _dummy_verify()
        return False

    if stored_hash.startswith("$argon2"):
        hasher = _hasher_for(stored_hash)
        try:
            return hasher.verify(stored_hash, raw_password)
        except VerifyMismatchError:
            return False
        except (VerificationError, InvalidHashError):
            return False
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected Argon2 verification failure")
            return False

    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")) and bcrypt is not None:
        try:
            candidate = raw_password.encode("utf-8")[:72]
            return bcrypt.checkpw(candidate, stored_hash.encode("utf-8"))
        except Exception:
            _dummy_verify()
            return False

    _dummy_verify()
    return False


def _dummy_verify() -> None:
    """Burn comparable CPU time so unknown users are not distinguishable."""
    try:
        hasher = _active_hasher()
        if hasher is _fast_hasher:
            _fast_hasher.verify(_FAST_DUMMY_HASH, "not-the-right-password")
        else:
            _hasher.verify(_SLOW_DUMMY_HASH, "not-the-right-password")
    except Exception:
        pass


def password_needs_rehash(stored_hash: str) -> bool:
    if not stored_hash:
        return True
    if stored_hash.startswith("$argon2"):
        try:
            return _hasher_for(stored_hash).check_needs_rehash(stored_hash)
        except InvalidHashError:
            return True
    # Any non-Argon2 hash is upgraded on next successful login.
    return True


def validate_password_strength(raw_password: str, *, user=None) -> None:
    """Raise ``PasswordError`` when a new password is not acceptable."""
    password = raw_password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if len(password) > 200:
        raise PasswordError("Password must be 200 characters or fewer.")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        raise PasswordError("That password is too common. Choose something unique.")
    if user is not None:
        username = (getattr(user, "username", "") or "").lower()
        email = (getattr(user, "email", "") or "").lower()
        full_name = (getattr(user, "full_name", "") or "").lower()
        if username and username in lowered:
            raise PasswordError("Password must not contain your username.")
        if email and email.split("@")[0] and email.split("@")[0] in lowered:
            raise PasswordError("Password must not contain your email address.")
        if full_name and len(full_name) > 3 and full_name in lowered:
            raise PasswordError("Password must not contain your name.")
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_letter and has_digit):
        raise PasswordError("Password must contain at least one letter and one number.")


# ---------------------------------------------------------------------------
# Opaque tokens (invitations, password resets). The raw value is emailed or
# shown once; only a SHA-256 hash is persisted.
# ---------------------------------------------------------------------------

def generate_token(nbytes: int = 32) -> str:
    """URL-safe random token. ~256 bits of entropy at the default size."""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw_token: str) -> str:
    import hashlib

    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def tokens_match(raw_token: str, stored_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    if not raw_token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(raw_token), stored_hash)


def default_token_expiry(minutes: int | None = None) -> datetime:
    if minutes is None:
        minutes = int(current_app.config.get("INVITE_TOKEN_TTL_MINUTES", 60 * 48))
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def reset_token_expiry(minutes: int | None = None) -> datetime:
    if minutes is None:
        minutes = int(current_app.config.get("RESET_TOKEN_TTL_MINUTES", 60))
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)
