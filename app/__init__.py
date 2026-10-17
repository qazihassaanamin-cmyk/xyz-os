"""Application factory for the XYZ Ltd. internal operating system."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import timedelta, timezone
from pathlib import Path

from flask import (
    Flask,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user
from werkzeug.exceptions import HTTPException

from app.config import CONFIG_MAP
from app.extensions import csrf, db, limiter, login_manager, migrate
from app.models.base import utcnow

# Repository root: two levels above app/__init__.py
BASE_DIR = Path(__file__).resolve().parent.parent


def _bump_asset_version() -> str:
    """Content hash of the CSS/JS bundles.

    Bumping the static version on every source change means a browser can never
    serve a stale stylesheet after a deploy, without any manual cache-busting.
    Computed once at import so the cost is paid a single time.
    """
    digest = hashlib.sha256()
    for folder, suffixes in (("css", {".css"}), ("js", {".js"})):
        root = BASE_DIR / "app" / "static" / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in suffixes:
                digest.update(path.name.encode("utf-8"))
                try:
                    digest.update(path.read_bytes())
                except OSError:  # pragma: no cover - unreadable file
                    continue
    return digest.hexdigest()[:10]


ASSET_VERSION = _bump_asset_version()

log = logging.getLogger(__name__)

# Endpoints reachable without an authenticated session. Everything else is
# protected globally by the guard in ``_enforce_login_first``.
PUBLIC_ENDPOINTS = {
    "auth.login",
    "auth.forgot_password",
    "auth.reset_password",
    "auth.accept_invite",
    "auth.health",
    "static",
}

PUBLIC_PREFIXES = ("/static/", "/favicon", "/robots.txt")


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    config_name = config_name or os.environ.get("FLASK_ENV", "development")
    config_class = CONFIG_MAP.get(config_name, CONFIG_MAP["development"])
    app.config.from_object(config_class)
    app.config["ENV_NAME"] = config_name
    # Read the database URL now, from this process's environment, so a .env
    # loaded late by the hosting platform or a test override still takes effect.
    from app.config import resolve_database_uri

    app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri()

    # A per-process fallback so a misconfigured dev box still starts; production
    # validation rejects a weak SECRET_KEY before we get here.
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = secrets.token_hex(32)

    # Session cookie: HttpOnly, SameSite=Lax, Secure in production, and a
    # rolling 8-hour lifetime that is refreshed on activity.
    app.config.setdefault("SESSION_REFRESH_EACH_REQUEST", True)

    _init_directories(app)
    _init_extensions(app)
    _init_blueprints(app)
    _init_login_manager(app)
    _init_error_handlers(app)
    _init_request_hooks(app)
    _init_cli(app)
    _init_template_globals(app)
    _apply_stored_settings(app)

    if config_name == "production":
        from app.config import ProductionConfig, ensure_secret_key

        # Materialise a persistent SECRET_KEY if one was not supplied, and
        # default COOKIE_SECURE to true — PythonAnywhere terminates HTTPS, so
        # secure cookies are safe there. A deploy that really needs otherwise
        # can still override both through the environment.
        ensure_secret_key()
        os.environ.setdefault("COOKIE_SECURE", "1")

        try:
            ProductionConfig.validate()
        except RuntimeError:
            app.logger.error("Refusing to start with an unsafe production configuration.")
            raise

    app.logger.info("%s started in %s mode", app.config.get("COMPANY_NAME", "Tavryc"), config_name)
    return app


def _apply_stored_settings(app: Flask) -> None:
    """Push database-backed settings into the Flask config at boot.

    Values the CEO changes in the UI take effect without a redeploy. If the
    settings table does not exist yet (a fresh database before ``flask db
    upgrade``) the stored overrides are simply skipped.
    """
    from app.services import settings_service

    try:
        settings_service.apply_runtime_overrides(app)
    except Exception:  # pragma: no cover - a fresh database has no settings yet
        app.logger.debug("Stored settings not applied yet.")


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _init_directories(app: Flask) -> None:
    upload_dir = app.config["UPLOAD_DIR"]
    if not os.path.isabs(upload_dir):
        upload_dir = os.path.join(BASE_DIR, upload_dir)
    app.config["UPLOAD_DIR_ABS"] = os.path.abspath(upload_dir)
    os.makedirs(app.config["UPLOAD_DIR_ABS"], exist_ok=True)
    os.makedirs(os.path.join(app.config["UPLOAD_DIR_ABS"], "avatars"), exist_ok=True)
    os.makedirs(os.path.join(app.config["UPLOAD_DIR_ABS"], "submissions"), exist_ok=True)
    instance_dir = os.path.join(BASE_DIR, "instance")
    os.makedirs(instance_dir, exist_ok=True)


_SESSION_TOUCH_SECONDS = 300


def _is_static_asset(path: str) -> bool:
    """True for anything Flask serves from the static folder.

    The login page is built from real stylesheets and scripts, so those assets
    must remain reachable to an anonymous visitor. Nothing else is.
    """
    static_url = app_static_url()
    return bool(static_url) and path.startswith(static_url)


def app_static_url() -> str:
    from flask import current_app

    try:
        return current_app.static_url_path or "/static"
    except Exception:  # pragma: no cover - outside an application context
        return "/static"


def _init_extensions(app: Flask) -> None:
    # An in-memory database needs a shared connection pool or every request would
    # receive an empty database of its own.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///:memory:"):
        from sqlalchemy.pool import StaticPool

        options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        options["poolclass"] = StaticPool
        options.pop("pool_pre_ping", None)  # meaningless for a static pool
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = options
    db.init_app(app)
    migrate.init_app(app, db, directory=str(BASE_DIR / "migrations"))
    csrf.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)

    # Import models so Alembic autogenerate sees every table.
    from app import models  # noqa: F401

    # Uploads must be served through authorized routes, never by Flask's static
    # handler. Enforce that at startup rather than trusting the config value.
    static_dir = os.path.abspath(app.static_folder or "")
    upload_dir = app.config["UPLOAD_DIR_ABS"]
    if static_dir and os.path.commonpath([static_dir, upload_dir]) == static_dir:
        raise RuntimeError(
            "UPLOAD_DIR must not live inside the static folder; uploads require authorization checks."
        )


def _init_blueprints(app: Flask) -> None:
    from app.announcements import bp as announcements_bp
    from app.auth import bp as auth_bp
    from app.ceo import bp as ceo_bp
    from app.daily import bp as daily_bp
    from app.dashboard import bp as dashboard_bp
    from app.focus import bp as focus_bp
    from app.inquiries import bp as inquiries_bp
    from app.knowledge import bp as knowledge_bp
    from app.lounge import bp as lounge_bp
    from app.main import bp as main_bp
    from app.notifications import bp as notifications_bp
    from app.projects import bp as projects_bp
    from app.suggestions import bp as suggestions_bp
    from app.tasks import bp as tasks_bp
    from app.team import bp as team_bp
    from app.uploads import bp as uploads_bp
    from app.xp import bp as xp_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp, url_prefix="/tasks")
    app.register_blueprint(projects_bp, url_prefix="/projects")
    app.register_blueprint(focus_bp, url_prefix="/focus")
    app.register_blueprint(daily_bp, url_prefix="/daily")
    app.register_blueprint(team_bp, url_prefix="/team")
    app.register_blueprint(notifications_bp, url_prefix="/notifications")
    app.register_blueprint(uploads_bp, url_prefix="/uploads")
    app.register_blueprint(suggestions_bp, url_prefix="/suggestions")
    app.register_blueprint(lounge_bp, url_prefix="/lounge")
    app.register_blueprint(announcements_bp, url_prefix="/announcements")
    app.register_blueprint(knowledge_bp, url_prefix="/knowledge")
    app.register_blueprint(xp_bp, url_prefix="/xp")
    app.register_blueprint(ceo_bp, url_prefix="/ceo")
    app.register_blueprint(inquiries_bp, url_prefix="/inquiries")


def _init_login_manager(app: Flask) -> None:
    from app.models.user import User

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    @login_manager.unauthorized_handler
    def unauthorized():
        if _wants_json():
            return jsonify({"error": "authentication_required"}), 401
        next_target = request.full_path if request.method == "GET" else "/"
        if not next_target.startswith("/") or next_target.startswith("//"):
            next_target = "/"
        flash("Please sign in to continue.", "info")
        return redirect(url_for("auth.login", next=next_target))


def _init_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(error):
        if _wants_json():
            return jsonify({"error": "forbidden"}), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(error):
        if _wants_json():
            return jsonify({"error": "not_found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(error):
        limit = current_app.config.get("MAX_UPLOAD_MB", 10)
        if _wants_json():
            return jsonify({"error": "file_too_large", "limit_mb": limit}), 413
        return render_template("errors/413.html", limit_mb=limit), 413

    @app.errorhandler(429)
    def rate_limited(error):
        if _wants_json():
            return jsonify({"error": "rate_limited"}), 429
        return render_template("errors/429.html"), 429

    @app.errorhandler(500)
    def server_error(error):
        log.exception("Unhandled server error on %s", request.path)
        db.session.rollback()
        if _wants_json():
            return jsonify({"error": "server_error"}), 500
        return render_template("errors/500.html"), 500

    # An unauthenticated visitor who follows a stale link should land on the
    # login page rather than being told the page is missing.
    @app.errorhandler(401)
    def unauthorized_error(error):
        if _wants_json():
            return jsonify({"error": "authentication_required"}), 401
        if _is_signed_in():
            return render_template("errors/403.html"), 401
        flash("Please sign in to continue.", "info")
        return redirect(url_for("auth.login"))

    @app.errorhandler(405)
    def method_not_allowed(error):
        if _wants_json():
            return jsonify({"error": "method_not_allowed"}), 405
        return render_template("errors/400.html"), 405


def _init_request_hooks(app: Flask) -> None:
    @app.before_request
    def _enforce_login_first():
        """Global login gate.

        The brief requires that an unauthenticated visitor sees the login page
        and nothing else. Navigation hiding is not enough, so the gate lives
        here and applies to every endpoint not explicitly listed as public.
        Static assets are exempt because the login page itself is built from
        them; no application data is reachable that way.
        """
        path = request.path or "/"
        if any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
            return None
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
            return None
        # Static assets are exempt because the login page itself is built from
        # them; no application data is reachable that way.
        if path.startswith(app_static_url()):
            return None
        if not _is_signed_in():
            if _wants_json():
                return jsonify({"error": "authentication_required"}), 401
            next_target = path
            if request.query_string:
                next_target = f"{path}?{request.query_string.decode('utf-8', 'ignore')}"
            if not next_target.startswith("/") or next_target.startswith("//"):
                next_target = "/"
            flash("Please sign in to continue.", "info")
            return redirect(url_for("auth.login", next=next_target))
        return None

    @app.before_request
    def _touch_session_activity():
        """Refresh the rolling session and touch the timer heartbeat.

        The session cookie is re-issued on activity so an eight-hour window
        rolls forward for someone who is actually using the application. The
        write is throttled: rewriting the cookie on every request would be pure
        overhead.
        """
        if not _is_signed_in():
            return None

        last = session.get("_last_touch")
        now = utcnow()
        if last is None or (now - last).total_seconds() > _SESSION_TOUCH_SECONDS:
            session["_last_touch"] = now
            session.modified = True
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "object-src 'none'",
        )
        if current_app.config.get("FORCE_HTTPS"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if _is_signed_in():
            response.headers.setdefault("Cache-Control", "no-store, private")
            response.headers.setdefault("Pragma", "no-cache")
        return response

    # Flask-WTF's default error response is a bare page; make CSRF failures
    # produce an understandable message instead of an opaque 400.
    @app.errorhandler(400)
    def bad_request(error):
        description = getattr(error, "description", "") or ""
        if "CSRF" in description.upper() or "csrf" in description:
            if _wants_json():
                return jsonify({"error": "csrf_failed"}), 400
            flash("Your session expired or the form was tampered with. Please try again.", "error")
            return redirect(url_for("auth.login") if not _is_signed_in() else request.path), 400
        if _wants_json():
            return jsonify({"error": "bad_request"}), 400
        return render_template("errors/400.html"), 400


def _init_cli(app: Flask) -> None:
    from app.cli import register_cli

    register_cli(app)


def _init_template_globals(app: Flask) -> None:
    @app.context_processor
    def inject_globals():
        from app.services.notification_service import unread_count_for

        count = 0
        nav_state = {
            "task_open_count": 0,
            "review_count": 0,
            "focus_running": False,
            "checkin_done": False,
            "attention_count": 0,
            "leading_projects": [],
        }
        if _is_signed_in():
            try:
                count = unread_count_for(current_user)
                nav_state = _nav_state_for(current_user)
            except Exception:  # pragma: no cover - never break a render
                app.logger.exception("Could not build navigation state")

        return {
            "company_name": current_app.config.get("COMPANY_NAME", "Tavryc"),
            "current_year": utcnow().year,
            "unread_notifications": count,
            "nav_active": request.endpoint or "",
            "asset_version": ASSET_VERSION,
            **nav_state,
        }

    @app.template_filter("localdate")
    def _localdate(value, fmt: str = "%d %b %Y"):
        """Render a stored UTC value as a date in the viewer's time zone."""
        local = _to_viewer_time(value)
        if local is None:
            return "—"
        try:
            return local.strftime(fmt)
        except Exception:
            return value

    @app.template_filter("localdatetime")
    def _localdatetime(value, fmt: str = "%d %b %Y, %H:%M"):
        local = _to_viewer_time(value)
        if local is None:
            return "—"
        try:
            return f"{local.strftime(fmt)} {local.tzname() or 'UTC'}"
        except Exception:
            return value

    @app.template_filter("localtime")
    def _localtime(value, fmt: str = "%H:%M"):
        local = _to_viewer_time(value)
        if local is None:
            return "—"
        try:
            return local.strftime(fmt)
        except Exception:
            return value

    @app.template_filter("localtz")
    def _localtz(value):
        local = _to_viewer_time(value)
        return (local.tzname() if local else None) or "UTC"

    @app.template_filter("relative")
    def _relative(value):
        if value is None:
            return "—"
        from app.utils.dates import humanise_delta

        return humanise_delta(value)

    @app.template_filter("initials")
    def _initials(user):
        return getattr(user, "initials", "?")

    @app.template_global()
    def company_today():
        from app.utils.dates import company_today as _ct

        return _ct()

    @app.template_global()
    def deadline_label(due):
        from app.utils.dates import deadline_label as _dl

        return _dl(due)


def _to_viewer_time(value):
    """Convert a stored UTC instant to the signed-in member's time zone.

    Storage is always UTC; presentation follows ``User.timezone``. Anonymous
    visitors fall back to the company time zone. Plain ``date`` values pass
    through unchanged — they carry no instant to convert.
    """
    if value is None:
        return None
    from datetime import date as _date, datetime as _datetime

    if isinstance(value, _date) and not isinstance(value, _datetime):
        return value
    if isinstance(value, str):
        from app.utils.dates import parse_iso

        value = parse_iso(value)
        if value is None:
            return value
    from app.utils.dates import company_tz

    target = company_tz()
    try:
        if current_user and getattr(current_user, "is_authenticated", False):
            zone_name = getattr(current_user, "timezone", None)
            if zone_name:
                from zoneinfo import ZoneInfo

                target = ZoneInfo(str(zone_name))
    except Exception:  # pragma: no cover - an invalid zone falls back to company time
        pass

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(target)


def _nav_state_for(user) -> dict:
    """Counts shown in the sidebar.

    Each count is a bounded query, and every one is scoped to what the member
    may actually see. The CEO's attention count reuses the same query the
    command centre uses so the number always matches the page.
    """
    from app.models.project import Project, ProjectMember, ProjectRole, ProjectStatus
    from app.models.task import Submission, SubmissionStatus, Task, TaskStatus
    from app.services import daily_service, focus_service, report_service
    from app.services.task_service import overdue_tasks_query, visible_tasks_query

    state = {
        "task_open_count": 0,
        "review_count": 0,
        "focus_running": False,
        "checkin_done": False,
        "attention_count": 0,
        "leading_projects": [],
    }

    state["task_open_count"] = (
        visible_tasks_query(user, include_completed=False)
        .filter(Task.assignee_id == user.id)
        .count()
    )

    if user.is_ceo:
        state["review_count"] = Submission.query.filter(
            Submission.status == SubmissionStatus.PENDING.value
        ).count()
    else:
        state["review_count"] = Submission.query.filter(
            Submission.status == SubmissionStatus.PENDING.value,
            Submission.task.has(Task.reviewer_id == user.id),
        ).count()

    state["focus_running"] = focus_service.current_session(user) is not None
    state["checkin_done"] = daily_service.get_checkin(user) is not None

    if user.is_ceo:
        state["attention_count"] = report_service.attention_queue(limit=50)["total"]
    else:
        state["attention_count"] = (
            overdue_tasks_query(user).filter(Task.assignee_id == user.id).count()
        )

    state["leading_projects"] = (
        ProjectMember.query.filter_by(
            user_id=user.id, role_in_project=ProjectRole.LEAD.value
        )
        .join(ProjectMember.project)
        .filter(Project.status != ProjectStatus.ARCHIVED.value)
        .limit(6)
        .all()
    )
    state["leading_projects"] = [row.project for row in state["leading_projects"]]

    return state


def _is_signed_in() -> bool:
    try:
        return bool(current_user and current_user.is_authenticated and current_user.is_active)
    except Exception:  # pragma: no cover - during early boot
        return False


def _wants_json() -> bool:
    if request.path.startswith("/api/") or request.path.endswith(".json"):
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"
