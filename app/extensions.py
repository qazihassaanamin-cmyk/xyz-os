"""Flask extension singletons.

Importing them from here (rather than from ``app``) keeps blueprints and
services free of circular imports.
"""

from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()


def _rate_limit_key() -> str:
    """Key for rate limiting.

    Prefers the authenticated user id so a member behind a shared office NAT
    cannot be throttled by a colleague's failed attempts, and falls back to the
    remote address for anonymous traffic.
    """
    from flask_login import current_user

    try:
        if current_user and getattr(current_user, "is_authenticated", False):
            return f"user:{current_user.id}"
    except Exception:  # pragma: no cover - defensive, never fatal
        pass
    return f"ip:{get_remote_address()}"


limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
