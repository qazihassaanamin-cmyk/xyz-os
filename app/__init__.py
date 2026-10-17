"""Application factory for the XYZ Ltd. internal operating system."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timezone

from flask import Flask, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user
from werkzeug.exceptions import HTTPException

from app.config import CONFIG_MAP
from app.extensions import csrf, db, limiter, login_manager, migrate

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

    # A per-process fallback so a misconfigured dev box still starts; production
    # validation rejects a weak SECRET_KEY before we get here.
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = secrets.token_hex(32)

    _init_directories(app)
    _init_extensions(app)
    _init_blueprints(app)
    _init_login_manager(app)
    _init_error_handlers(app)
    _init_request_hooks(app)
    _init_cli(app)
    _init_template_globals(app)

    if config_name == "production":
        try:
            from app.config import ProductionConfig

            ProductionConfig.validate()
        except RuntimeError:
            app.logger.error("Refusing to start with an unsafe production configuration.")
            raise

    app.logger.info("%s started in %s mode", app.config.get("COMPANY_NAME", "Tavryc"), config_name)
    return app


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _init_directories(app: Flask) -> None:
    upload_dir = app.config["UPLOAD_DIR"]
    if not os.path.isabs(upload_dir):
        upload_dir = os.path.join(app.root_path, "..", upload_dir)
    app.config["UPLOAD_DIR_ABS"] = os.path.abspath(upload_dir)
    os.makedirs(app.config["UPLOAD_DIR_ABS"], exist_ok=True)
    os.makedirs(os.path.join(app.config["UPLOAD_DIR_ABS"], "avatars"), exist_ok=True)
    os.makedirs(os.path.join(app.config["UPLOAD_DIR_ABS"], "submissions"), exist_ok=True)
    instance_dir = os.path.join(os.path.dirname(app.root_path), "instance")
    os.makedirs(instance_dir, exist_ok=True)


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(app.root_path), "migrations"))
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

    @app.errorhandler(Exception)
    def unhandled(error):
        if isinstance(error, HTTPException):
            return error
        log.exception("Unhandled exception on %s", request.path)
        db.session.rollback()
        if _wants_json():
            return jsonify({"error": "server_error"}), 500
        return render_template("errors/500.html"), 500


def _init_request_hooks(app: Flask) -> None:
    @app.before_request
    def _enforce_login_first():
        """Global login gate.

        The brief requires that an unauthenticated visitor sees the login page
        and nothing else. Navigation hiding is not enough, so the gate lives
        here and applies to every endpoint not explicitly listed as public.
        """
        path = request.path or "/"
        if any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
            return None
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
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
        from flask_login import current_user as _cu

        if _cu and _cu.is_authenticated:
            session["_last_seen"] = request.path
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
    from app.models.base import utcnow

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
            **nav_state,
        }

    @app.template_filter("localdate")
    def _localdate(value, fmt: str = "%d %b %Y"):
        if value is None:
            return "—"
        try:
            return value.strftime(fmt)
        except Exception:
            return value

    @app.template_filter("localdatetime")
    def _localdatetime(value, fmt: str = "%d %b %Y, %H:%M"):
        if value is None:
            return "—"
        try:
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.strftime(fmt) + " UTC"
        except Exception:
            return value

    @app.template_filter("relative")
    def _relative(value):
        if value is None:
            return "—"
        from app.utils.dates import humanise_delta

        return humanise_delta(value)

    @app.template_filter("initials")
    def _initials(user):
        return getattr(user, "initials", "?")


def _nav_state_for(user) -> dict:
    """Counts shown in the sidebar.

    Each count is a bounded query, and every one is scoped to what the member
    may actually see. The CEO's attention count reuses the same query the
    command centre uses so the number always matches the page.
    """
    from app.models.project import ProjectMember, ProjectRole
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
        .filter(db.text("projects.status != 'ARCHIVED'"))
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
