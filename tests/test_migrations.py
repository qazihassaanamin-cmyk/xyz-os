"""The Alembic chain must upgrade a blank database to the current schema.

This is the one place the migration chain is exercised. The per-test fixtures
build their schema with ``create_all()`` for speed; this test proves the
migrations themselves stay in step with the models by upgrading a real blank
database through Alembic and comparing the resulting tables.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_migrations_upgrade_blank_database():
    """Every migration applies to a fresh SQLite database without error."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    with tempfile.TemporaryDirectory(prefix="tavryc-mig-") as directory:
        database_path = os.path.join(directory, "mig.sqlite")
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"
        try:
            from app import create_app

            application = create_app("testing")
            application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database_path}"
            with application.app_context():
                config = Config(str(ROOT / "migrations" / "alembic.ini"))
                config.set_main_option("script_location", str(ROOT / "migrations"))
                command.upgrade(config, "head")

                inspector = inspect(application.extensions["db"].engine)
                tables = set(inspector.get_table_names())
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous

    # The baseline ships these tables; if a migration drifts from the models
    # this fails loudly rather than silently.
    required = {
        "users", "xp_profiles", "xp_transactions", "user_xp_summary",
        "projects", "project_members", "tasks", "submissions", "reviews",
        "blockers", "milestones", "focus_sessions", "workdays",
        "workday_exceptions", "daily_checkins", "daily_reports",
        "notifications", "lounge_posts", "lounge_post_revisions",
        "suggestions", "suggestion_votes", "knowledge_items",
        "announcements", "achievements", "member_achievements",
        "member_badges", "attachments", "audit_log", "login_attempts",
        "settings", "level_definitions", "xp_rules", "challenges",
        "challenge_progress", "activity_log", "alembic_version",
    }
    missing = required - tables
    assert not missing, f"Migrations did not create: {sorted(missing)}"


def test_lounge_posts_has_topic_column():
    """The latest migration adds the topic column to lounge_posts."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    with tempfile.TemporaryDirectory(prefix="tavryc-mig2-") as directory:
        database_path = os.path.join(directory, "mig2.sqlite")
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"
        try:
            from app import create_app

            application = create_app("testing")
            application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database_path}"
            with application.app_context():
                config = Config(str(ROOT / "migrations" / "alembic.ini"))
                config.set_main_option("script_location", str(ROOT / "migrations"))
                command.upgrade(config, "head")

                inspector = inspect(application.extensions["db"].engine)
                columns = {col["name"] for col in inspector.get_columns("lounge_posts")}
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous

    assert "topic" in columns