"""Application configuration classes.

All environment-specific behaviour is switched here so the rest of the code
base never branches on ``FLASK_ENV``. Secrets are read from the environment
only; the defaults below are development-safe placeholders and must never be
used to run a real deployment.
"""

from __future__ import annotations

import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class BaseConfig:
    """Settings shared by every environment."""

    # --- Core -----------------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-secret-key-change-me")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "Tavryc")
    COMPANY_TZ = os.environ.get("COMPANY_TZ", "Asia/Karachi")

    # --- Database -------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "tavryc.sqlite")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

    # --- Sessions / cookies --------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("COOKIE_SECURE", False)
    SESSION_COOKIE_NAME = "tavryc_session"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_int("SESSION_LIFETIME_HOURS", 8))
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _bool("COOKIE_SECURE", False)
    REMEMBER_COOKIE_DURATION = timedelta(days=14)

    # --- CSRF -----------------------------------------------------------
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None  # tied to the session instead of a hard clock
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]

    # --- Uploads --------------------------------------------------------
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
    MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 10)
    MAX_FILES_PER_SUBMISSION = _int("MAX_FILES_PER_SUBMISSION", 5)
    MAX_CONTENT_LENGTH = _int("MAX_UPLOAD_MB", 10) * 1024 * 1024 * 12

    # --- Security headers ----------------------------------------------
    FORCE_HTTPS = _bool("FORCE_HTTPS", False)
    PROXY_FIX_ENABLED = _bool("PROXY_FIX_ENABLED", False)
    TRUSTED_PROXY_COUNT = _int("TRUSTED_PROXY_COUNT", 1)

    # --- Rate limiting --------------------------------------------------
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True

    # --- Mail (optional) -------------------------------------------------
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = _int("SMTP_PORT", 587)
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS = _bool("SMTP_USE_TLS", True)
    MAIL_FROM = os.environ.get("MAIL_FROM", "no-reply@tavryc.com")
    ALLOW_INSECURE_DEV_LINKS = _bool("ALLOW_INSECURE_DEV_LINKS", True)

    ANON_VOTE_SALT = os.environ.get("ANON_VOTE_SALT", "dev-anon-salt-change-me")

    # --- Behaviour limits (overridable at runtime from the settings table)
    FOCUS_MIN_SECONDS = 20 * 60          # a completed focus session requires this
    FOCUS_PAUSE_IDLE_LIMIT_SECONDS = 30 * 60
    FOCUS_RUNNING_STALE_SECONDS = 45 * 60
    LOUNGE_EDIT_WINDOW_SECONDS = 10 * 60
    UPLOAD_DELETE_WINDOW_SECONDS = 15 * 60
    REPORT_EDIT_CUTOFF_HOUR = _int("REPORT_EDIT_CUTOFF_HOUR", 23)

    DEFAULT_WORKWEEK = "1,2,3,4,5"       # ISO weekday numbers, Monday = 1

    # --- Deployment -----------------------------------------------------
    PREFERRED_URL_SCHEME = "https" if _bool("FORCE_HTTPS", False) else "http"
    # PythonAnywhere free tier: 100 CPU-seconds/day and 512 MiB of disk. Keep
    # expensive work off the request path and keep uploads small.
    PYTHONANYWHERE_FREE_TIER = _bool("PYTHONANYWHERE_FREE_TIER", False)

    # --- Gamification toggles (rule values live in the database) ---------
    GAMIFICATION_ENABLED = _bool("GAMIFICATION_ENABLED", True)

    JSON_SORT_KEYS = False
    TEMPLATES_AUTO_RELOAD = False

    @staticmethod
    def _db_is_sqlite(uri: str) -> bool:
        return uri.startswith("sqlite")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    TEMPLATES_AUTO_RELOAD = True
    SQLALCHEMY_ECHO = False
    # Development or single-server deployment: SQLite, no HTTPS terminator.
    # Suitable for PythonAnywhere's free tier.
    PREFERRED_URL_SCHEME = "http"


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    WTF_CSRF_ENABLED = True  # tests explicitly exercise CSRF behaviour
    SERVER_NAME = "localhost"
    PREFERRED_URL_SCHEME = "http"
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"
    FOCUS_MIN_SECONDS = 20 * 60


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = _bool("COOKIE_SECURE", True)
    REMEMBER_COOKIE_SECURE = _bool("COOKIE_SECURE", True)
    PREFERRED_URL_SCHEME = "https"
    SESSION_COOKIE_NAME = "__Host-tavryc_session" if _bool("FORCE_HTTPS", False) else "tavryc_session"

    @classmethod
    def validate(cls) -> None:
        """Fail fast on an unsafe production configuration."""
        problems = []
        if not os.environ.get("SECRET_KEY") or "change-me" in cls.SECRET_KEY:
            problems.append("SECRET_KEY must be set to a strong unique value.")
        if "sqlite" in cls.SQLALCHEMY_DATABASE_URI:
            problems.append("DATABASE_URL should point at PostgreSQL in production.")
        if not _bool("COOKIE_SECURE", False):
            problems.append("COOKIE_SECURE must be true in production.")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + " ".join(problems))


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
