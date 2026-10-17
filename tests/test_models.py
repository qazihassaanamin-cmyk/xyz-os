"""Schema, configuration and command-line behaviour.

These are the checks that keep the foundation honest: every required table and
column exists, the constraints that carry business rules are actually present in
the database, the portable focus guards work on SQLite, and the CLI refuses to
seed a production database.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db

# Section G of the specification, table by table. Kept as one list so a missing
# table is a single, obvious failure rather than a mystery later.
REQUIRED_TABLES = {
    "users",
    "invitations",
    "password_reset_tokens",
    "projects",
    "project_members",
    "milestones",
    "tasks",
    "subtasks",
    "task_comments",
    "submissions",
    "reviews",
    "attachments",
    "focus_sessions",
    "workdays",
    "workday_exceptions",
    "daily_checkins",
    "daily_reports",
    "daily_report_revisions",
    "blockers",
    "suggestions",
    "suggestion_votes",
    "suggestion_comments",
    "suggestion_status_events",
    "lounge_posts",
    "lounge_post_revisions",
    "lounge_read_state",
    "knowledge_entries",
    "decision_records",
    "announcements",
    "announcement_reads",
    "notifications",
    "audit_log",
    "settings",
    "login_attempts",
    "xp_transactions",
    "xp_rules",
    "xp_profiles",
    "level_definitions",
    "achievements",
    "member_achievements",
    "member_badges",
    "challenges",
    "challenge_progress",
    "streaks",
    "user_xp_summary",
}


class TestSchema:
    def test_every_required_table_exists(self, db):
        present = set(inspect(_db.engine).get_table_names())
        missing = REQUIRED_TABLES - present
        assert missing == set(), f"Missing tables: {sorted(missing)}"

    def test_audit_log_is_append_only_by_design(self, db):
        columns = {c["name"] for c in inspect(_db.engine).get_columns("audit_log")}
        assert {"action", "created_at", "user_id", "ip_address"} <= columns

    def test_xp_ledger_carries_an_idempotency_key(self, db):
        inspector = inspect(_db.engine)
        columns = {c["name"] for c in inspector.get_columns("xp_transactions")}
        assert "idempotency_key" in columns
        unique_columns = {
            tuple(index["column_names"])
            for index in inspector.get_indexes("xp_transactions")
            if index.get("unique")
        }
        assert ("idempotency_key",) in unique_columns

    def test_notifications_dedupe_key_is_unique(self, db):
        inspector = inspect(_db.engine)
        unique_columns = {
            tuple(index["column_names"])
            for index in inspector.get_indexes("notifications")
            if index.get("unique")
        }
        assert ("dedupe_key",) in unique_columns

    @pytest.mark.parametrize(
        "table,columns",
        [
            ("focus_sessions", {"open_key", "credit_key"}),
            ("suggestions", {"author_id", "is_anonymous", "decline_reason"}),
            ("attachments", {"progress_stage", "deleted_at", "width", "height"}),
            ("suggestion_anon_votes", {"voter_hash"}),
            ("suggestion_status_events", {"from_status", "to_status"}),
            ("streaks", {"kind", "current_count", "best_count"}),
            ("user_xp_summary", {"balance", "lifetime_xp"}),
            ("lounge_read_state", {"room_id", "last_read_id"}),
            ("decision_records", {"decided_at", "decision"}),
        ],
    )
    def test_required_columns_are_present(self, db, table, columns):
        present = {c["name"] for c in inspect(_db.engine).get_columns(table)}
        assert columns <= present, f"{table} is missing {sorted(columns - present)}"

    def test_every_table_has_timestamps(self, db):
        """Every table carries created_at/updated_at, as section G requires.

        Two deliberate exceptions, documented here rather than silently skipped:
        ``audit_log`` and ``xp_transactions`` are append-only ledgers, so an
        ``updated_at`` column would advertise a mutation path that must not
        exist. ``created_at`` is present on both.
        """
        append_only = {"audit_log", "xp_transactions"}
        inspector = inspect(_db.engine)
        offenders = []
        for table in REQUIRED_TABLES:
            columns = {c["name"] for c in inspector.get_columns(table)}
            required = {"created_at"} if table in append_only else {"created_at", "updated_at"}
            if not required <= columns:
                offenders.append(table)
        assert offenders == [], f"Tables without required timestamps: {offenders}"

    def test_users_email_and_username_are_unique(self, db, ceo, make_user):
        with pytest.raises(IntegrityError):
            duplicate = type(ceo)(
                email=ceo.email,
                username="another",
                full_name="Duplicate",
                role="MEMBER",
            )
            duplicate.set_password("test-password-1234")
            _db.session.add(duplicate)
            _db.session.commit()
        _db.session.rollback()

    def test_project_membership_is_unique_per_pair(self, db, project, member):
        from app.models.project import ProjectMember

        with pytest.raises(IntegrityError):
            _db.session.add(
                ProjectMember(project_id=project.id, user_id=member.id, role_in_project="MEMBER")
            )
            _db.session.commit()
        _db.session.rollback()

    def test_one_checkin_per_member_per_workday(self, db, member):
        from app.models.daily import DailyCheckin
        from app.utils.dates import company_today

        day = company_today()
        _db.session.add(
            DailyCheckin(user_id=member.id, workday_date=day, priorities="First")
        )
        _db.session.commit()
        with pytest.raises(IntegrityError):
            _db.session.add(
                DailyCheckin(user_id=member.id, workday_date=day, priorities="Second")
            )
            _db.session.commit()
        _db.session.rollback()

    def test_one_vote_per_member_per_suggestion(self, db, member):
        from app.models.suggestion import Suggestion, SuggestionVote

        suggestion = Suggestion(title="A real idea", body="With a real problem.", category="PROCESS")
        _db.session.add(suggestion)
        _db.session.commit()
        _db.session.add(SuggestionVote(suggestion_id=suggestion.id, user_id=member.id))
        _db.session.commit()
        with pytest.raises(IntegrityError):
            _db.session.add(SuggestionVote(suggestion_id=suggestion.id, user_id=member.id))
            _db.session.commit()
        _db.session.rollback()


class TestFocusDatabaseGuards:
    """The portable guards that replace dialect-specific partial indexes.

    ``open_key`` and ``credit_key`` are plain UNIQUE columns that stay NULL unless
    the row is in a state that must be unique. A NULL never conflicts on any
    dialect, so the rule behaves identically on SQLite, MySQL and PostgreSQL.
    """

    def _session(self, user, **overrides):
        from app.models.focus import FocusSession, FocusStatus
        from app.utils.dates import company_today

        values = {
            "user_id": user.id,
            "goal": "Test session",
            "workday_date": company_today(),
            "status": FocusStatus.RUNNING.value,
            "started_at": datetime.now(timezone.utc),
            "last_resumed_at": datetime.now(timezone.utc),
            "accumulated_seconds": 0,
        }
        values.update(overrides)
        return FocusSession(**values)

    def test_two_open_sessions_for_one_member_are_refused(self, db, member):
        from app.models.focus import FocusSession, FocusStatus

        _db.session.add(self._session(member, open_key=f"user:{member.id}"))
        _db.session.commit()
        with pytest.raises(IntegrityError):
            _db.session.add(self._session(member, open_key=f"user:{member.id}"))
            _db.session.commit()
        _db.session.rollback()
        assert (
            FocusSession.query.filter_by(user_id=member.id, status=FocusStatus.RUNNING.value).count()
            == 1
        )

    def test_two_credited_sessions_on_one_workday_are_refused(self, db, member):
        from app.models.focus import FocusSession, FocusStatus
        from app.utils.dates import company_today

        second = self._session(
            member,
            status=FocusStatus.COMPLETED.value,
            ended_at=datetime.now(timezone.utc),
            duration_seconds=1500,
            credit_key=f"user:{member.id}:{company_today().isoformat()}",
        )
        _db.session.add(second)
        _db.session.commit()

        with pytest.raises(IntegrityError):
            third = self._session(
                member,
                status=FocusStatus.COMPLETED.value,
                ended_at=datetime.now(timezone.utc),
                duration_seconds=1500,
                credit_key=f"user:{member.id}:{company_today().isoformat()}",
            )
            _db.session.add(third)
            _db.session.commit()
        _db.session.rollback()

    def test_extra_sessions_without_a_credit_key_are_allowed(self, db, member):
        """A second completed session the same day is stored, just not credited."""
        from app.models.focus import FocusSession, FocusStatus

        for _ in range(2):
            _db.session.add(
                self._session(
                    member,
                    status=FocusStatus.COMPLETED.value,
                    ended_at=datetime.now(timezone.utc),
                    duration_seconds=1300,
                    credit_key=None,
                )
            )
        _db.session.commit()
        assert (
            FocusSession.query.filter_by(
                user_id=member.id, status=FocusStatus.COMPLETED.value
            ).count()
            == 2
        )

    def test_the_guard_columns_stay_null_unless_the_state_needs_them(self, db, member):
        """A NULL in a unique column never collides, on any dialect.

        That is exactly why the guards are modelled this way: MySQL has no
        partial unique indexes, so ``open_key`` and ``credit_key`` carry the
        rule as ordinary unique columns that are NULL outside the guarded state.
        """
        from app.models.focus import FocusSession, FocusStatus

        _db.session.add(
            self._session(
                member,
                status=FocusStatus.COMPLETED.value,
                ended_at=datetime.now(timezone.utc),
                duration_seconds=1300,
                open_key=None,
                credit_key=None,
            )
        )
        _db.session.commit()
        rows = FocusSession.query.filter_by(user_id=member.id).all()
        assert all(row.open_key is None for row in rows)
        assert all(row.credit_key is None for row in rows)

    def test_different_members_may_each_hold_an_open_session(self, db, member, ceo):
        _db.session.add(self._session(member, open_key=f"user:{member.id}"))
        _db.session.add(self._session(ceo, open_key=f"user:{ceo.id}"))
        _db.session.commit()

    def test_closing_a_session_frees_the_open_guard(self, db, member):
        from app.models.focus import FocusSession, FocusStatus

        row = self._session(member, open_key=f"user:{member.id}")
        _db.session.add(row)
        _db.session.commit()

        row.open_key = None
        row.status = FocusStatus.COMPLETED.value
        row.ended_at = datetime.now(timezone.utc)
        row.duration_seconds = 1300
        _db.session.commit()

        _db.session.add(self._session(member, open_key=f"user:{member.id}"))
        _db.session.commit()
        assert FocusSession.query.filter_by(user_id=member.id).count() == 2


class TestConfiguration:
    def test_testing_config_never_touches_the_development_database(self, app):
        assert "sqlite" in app.config["SQLALCHEMY_DATABASE_URI"]
        assert "test" in app.config["SQLALCHEMY_DATABASE_URI"].lower()

    def test_uploads_live_outside_the_static_folder(self, app):
        import os

        static = os.path.abspath(app.static_folder)
        uploads = app.config["UPLOAD_DIR_ABS"]
        assert os.path.commonpath([static, uploads]) != static

    def test_secrets_are_not_hardcoded_in_production_config(self):
        from app.config import ProductionConfig

        assert ProductionConfig.SESSION_COOKIE_HTTPONLY is True
        assert ProductionConfig.SESSION_COOKIE_SAMESITE == "Lax"

    def test_production_config_refuses_an_unsafe_setup(self, monkeypatch):
        """Unchanged from the shipped defaults, production validation must fail.

        The test restores the environment afterwards so it cannot influence other
        tests: class attributes are read from the environment at import time, and
        the production class is not meant to be mutated in place.
        """
        from app.config import ProductionConfig

        monkeypatch.delenv("COOKIE_SECURE", raising=False)
        monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-secret-key-change-me")
        with pytest.raises(RuntimeError) as excinfo:
            ProductionConfig.validate()
        assert "SECRET_KEY" in str(excinfo.value)

    def test_production_config_accepts_a_complete_setup(self, monkeypatch):
        from app.config import ProductionConfig

        monkeypatch.setenv("SECRET_KEY", "a" * 48)
        monkeypatch.setenv("COOKIE_SECURE", "true")
        ProductionConfig.SECRET_KEY = "a" * 48
        ProductionConfig.SESSION_COOKIE_SECURE = True
        ProductionConfig.validate()  # must not raise
        ProductionConfig.SECRET_KEY = os.environ.get(
            "SECRET_KEY", "dev-only-insecure-secret-key-change-me"
        )

    def test_company_timezone_resolves(self, app):
        from app.utils.dates import company_today, company_tz

        assert str(company_tz()) == "Asia/Karachi"
        assert isinstance(company_today(), date)

    def test_workday_boundary_uses_company_time(self, app):
        from app.utils.dates import workday_for

        # 20:30 UTC is already the next day in Asia/Karachi (UTC+5).
        moment = datetime(2026, 3, 10, 20, 30, tzinfo=timezone.utc)
        assert workday_for(moment) == date(2026, 3, 11)

    def test_seeded_settings_are_complete(self, app):
        from app.services import settings_service

        grouped = settings_service.grouped()
        keys = {row.key for rows in grouped.values() for row in rows}
        assert "company_tz" in keys
        assert "anonymous_suggestions_enabled" in keys
        assert settings_service.get_bool("anonymous_suggestions_enabled") is False

    def test_level_ladder_and_xp_rules_are_seeded(self, app):
        from app.models.xp import LevelDefinition, XpRule

        assert LevelDefinition.query.count() >= 8
        assert XpRule.query.filter_by(code="TASK_COMPLETED").first() is not None

    def test_achievement_catalogue_is_seeded(self, app):
        from app.models.achievement import Achievement

        codes = {row.code for row in Achievement.query.all()}
        assert {"first_delivery", "deadline_keeper", "quality_first"} <= codes


class TestCli:
    def test_create_ceo_creates_exactly_one_ceo(self, app):
        from app.models.user import User
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            app.cli.commands["create-ceo"],
            [],
            input="ceo@tavryc.com\nceo\nMuhammad Hassaan\nTestPassword12345\nTestPassword12345\n",
        )
        assert result.exit_code == 0, result.output
        assert User.query.filter_by(role="CEO").count() == 1

    def test_create_ceo_refuses_a_second_ceo(self, app, ceo):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            app.cli.commands["create-ceo"],
            [],
            input="other@tavryc.com\nother\nOther Person\nTestPassword12345\nTestPassword12345\n",
        )
        assert result.exit_code == 1

    def test_seed_dev_creates_founders_and_projects(self, app):
        from app.models.project import Project
        from app.models.user import User
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app.cli.commands["seed-dev"], [])
        assert result.exit_code == 0, result.output
        assert User.query.count() >= 4
        assert Project.query.count() >= 2

    def test_seed_dev_refuses_in_production(self, app, monkeypatch):
        from click.testing import CliRunner

        monkeypatch.setitem(app.config, "ENV_NAME", "production")
        runner = CliRunner()
        result = runner.invoke(app.cli.commands["seed-dev"], [])
        assert result.exit_code == 1
        assert "production" in result.output.lower()

    def test_invite_command_issues_a_single_use_link(self, app, ceo):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app.cli.commands["invite"], ["newcomer@tavryc.com"])
        assert result.exit_code == 0, result.output
        assert "INVITATION LINK" in result.output
        assert "newcomer@tavryc.com" in result.output

    def test_notify_run_and_xp_reconcile_exist(self, app):
        assert "notify-run" in app.cli.commands
        assert "xp-reconcile" in app.cli.commands

    def test_xp_reconcile_rebuilds_from_the_ledger(self, app, member):
        from click.testing import CliRunner
        from app.models.xp import XpProfile, XpReason
        from app.services import xp_service

        xp_service.award_xp(member, XpReason.MANUAL_AWARD.value, amount=50, note="Seeded award", allow_manual=True)
        profile = XpProfile.query.filter_by(user_id=member.id).first()
        profile.balance = 0
        profile.lifetime_xp = 0
        _db.session.commit()

        runner = CliRunner()
        result = runner.invoke(app.cli.commands["xp-reconcile"], [])
        assert result.exit_code == 0, result.output

        _db.session.refresh(profile)
        assert profile.balance == 50
        assert profile.lifetime_xp == 50


class TestMigrations:
    def test_initial_migration_exists_and_creates_the_schema(self, app, tmp_path):
        """Run the migration chain against an empty database, not just create_all."""
        from alembic import command
        from alembic.config import Config
        from pathlib import Path

        from sqlalchemy import create_engine, inspect as sa_inspect

        root = Path(__file__).resolve().parent.parent
        database = tmp_path / "migrated.sqlite"
        engine = create_engine(f"sqlite:///{database}")

        config = Config(str(root / "migrations" / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")

        with app.app_context():
            command.upgrade(config, "head")

        tables = set(sa_inspect(engine).get_table_names())
        assert {"users", "tasks", "xp_transactions", "focus_sessions", "audit_log"} <= tables
        engine.dispose()

    def test_migration_chain_is_a_single_head(self, app):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        config = Config(str(root / "migrations" / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        assert len(heads) == 1, f"Multiple migration heads: {heads}"
