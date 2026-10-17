"""Shared pytest fixtures.

Every test runs against a throwaway SQLite database in a temporary directory, so
the development and production databases are never touched. Each test gets a
fresh schema and a fresh upload directory.

One application context stays pushed for the duration of each test. That keeps
ORM objects usable across fixture boundaries and mirrors how a real request runs
inside a single context.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Configure the test environment before the application is imported.
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "testing-secret-key-not-used-in-production")
os.environ.setdefault("ANON_VOTE_SALT", "testing-anon-salt")

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402

TEST_PASSWORD = "test-password-1234"


@pytest.fixture()
def app():
    """A fresh application, database and upload directory for each test."""
    with tempfile.TemporaryDirectory(prefix="tavryc-test-") as directory:
        database_path = os.path.join(directory, "test.sqlite")
        upload_path = os.path.join(directory, "uploads")
        os.makedirs(upload_path, exist_ok=True)

        # Point the application at the throwaway database *before* it is built.
        # create_app() reads stored settings at boot, and if it did that against
        # the development database the suite would touch real data.
        previous_database = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"
        try:
            application = create_app("testing")
        finally:
            if previous_database is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_database

        application.config.update(
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
            UPLOAD_DIR=upload_path,
            UPLOAD_DIR_ABS=upload_path,
            WTF_CSRF_ENABLED=True,
            SERVER_NAME="localhost",
            RATELIMIT_ENABLED=False,
            TESTING=True,
            PROPAGATE_EXCEPTIONS=True,
        )

        with application.app_context():
            # Build the schema with create_all() rather than the Alembic chain.
            # The migration chain is exercised by
            # ``tests/test_migrations.py`` against a real upgrade; per-test
            # schema construction here is about speed, and the test database is
            # throwaway either way.
            _db.create_all()
            _seed_reference_data()
            yield application
            _db.session.remove()
            _db.engine.dispose()


def _seed_reference_data() -> None:
    """Levels, XP rules, achievements and settings — the shipped configuration."""
    from app.services import settings_service
    from app.services.achievement_service import ensure_catalogue
    from app.services.level_service import ensure_ladder
    from app.services.xp_service import ensure_rules

    settings_service.ensure_defaults()
    ensure_ladder()
    ensure_rules()
    ensure_catalogue()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return _db


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_user(app):
    """Factory that creates an account and returns the live ORM object."""
    from app.models.user import User
    from app.services.xp_service import ensure_profile

    def _make(
        username="member",
        email=None,
        role="MEMBER",
        full_name=None,
        password=TEST_PASSWORD,
        is_active=True,
        title=None,
    ):
        user = User(
            username=username,
            email=email or f"{username}@tavryc.com",
            full_name=full_name or username.title(),
            role=role,
            title=title,
            is_active_flag=is_active,
            timezone="Asia/Karachi",
        )
        user.set_password(password)
        _db.session.add(user)
        _db.session.commit()
        ensure_profile(user)
        return user

    _make.password = TEST_PASSWORD
    return _make


@pytest.fixture()
def ceo(make_user):
    return make_user(
        username="ceo",
        email="ceo@tavryc.com",
        role="CEO",
        full_name="Muhammad Hassaan",
        title="Caretaker CEO and Technical Lead",
    )


@pytest.fixture()
def member(make_user):
    return make_user(
        username="saad",
        email="saad@tavryc.com",
        role="MEMBER",
        full_name="Saad Zaheer",
        title="Business and Commercial Lead",
    )


@pytest.fixture()
def other_member(make_user):
    return make_user(
        username="ilyas",
        email="ilyas@tavryc.com",
        role="MEMBER",
        full_name="Muhammad Ilyas",
        title="Company-Wide Oversight Lead",
    )


@pytest.fixture()
def third_member(make_user):
    return make_user(
        username="somman",
        email="somman@tavryc.com",
        role="MEMBER",
        full_name="Somman Umer",
        title="Business Operations and Technical Lead",
    )


@pytest.fixture()
def login(secure_client):
    """Sign in through the real login route, with a valid CSRF token."""

    def _login(identifier: str, password: str = TEST_PASSWORD, **extra):
        payload = {"identifier": identifier, "password": password}
        payload.update(extra)
        return secure_client.post("/login", data=payload, follow_redirects=False)

    return _login


@pytest.fixture()
def auth(secure_client):
    """Sign a user in directly, for tests that are not about the login form.

    Flask-Login's strong session protection also binds the session to a
    fingerprint of the request, so the direct session write includes one; that
    keeps the shortcut faithful to what a real sign-in produces.
    """

    def _auth(user):
        client = secure_client._client
        with client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
            session["_id"] = _session_fingerprint()
        return secure_client

    return _auth


def _session_fingerprint() -> str:
    """The identifier Flask-Login's strong protection stores."""
    import hashlib

    from flask import request

    from app.extensions import login_manager

    identifier = login_manager._session_identifier_generator()
    return hashlib.sha512(identifier.encode("utf-8")).hexdigest()


@pytest.fixture()
def csrf_token():
    """Read the CSRF token as a browser would: from the rendered form."""
    import re

    pattern = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')

    def _token(client):
        for url in ("/login", "/"):
            body = client.get(url).get_data(as_text=True)
            match = pattern.search(body)
            if match:
                return match.group(1)
        with client.session_transaction() as session:
            return session.get("csrf_token")

    return _token


@pytest.fixture()
def secure_client(client):
    """A test client that submits a real CSRF token with every form post.

    CSRF protection is a real part of this application, so tests exercise it the
    way a browser does rather than switching it off. Tests that specifically
    assert CSRF rejection use the plain ``client`` fixture instead.

    Flask-WTF 1.2 stores the raw secret in the session but renders the encrypted
    token in the form, so the token submitted to the server must be the rendered
    value. Reading it back out of the session would submit the wrong string.
    """
    import re

    pattern = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')

    class _SecureClient:
        def __init__(self, wrapped):
            self._client = wrapped

        @property
        def application(self):
            return self._client.application

        def _token(self):
            for url in ("/login", "/"):
                body = self._client.get(url).get_data(as_text=True)
                match = pattern.search(body)
                if match:
                    return match.group(1)
            with self._client.session_transaction() as session:
                return session.get("csrf_token")

        def post(self, url, data=None, **kwargs):
            payload = dict(data or {})
            # Only set a token when the caller has not supplied one, so tests can
            # deliberately submit a missing or wrong token.
            if "csrf_token" not in payload:
                payload["csrf_token"] = self._token()
            return self._client.post(url, data=payload, **kwargs)

        def open(self, url, **kwargs):
            """A plain GET that also establishes a session and a CSRF token."""
            response = self._client.get(url, **kwargs)
            self._token()
            return response

        def get(self, *args, **kwargs):
            return self._client.get(*args, **kwargs)

        def put(self, url, data=None, **kwargs):
            payload = dict(data or {})
            payload.setdefault("csrf_token", self._token())
            return self._client.put(url, data=payload, **kwargs)

        def delete(self, url, **kwargs):
            kwargs.setdefault("query_string", {"csrf_token": self._token()})
            return self._client.delete(url, **kwargs)

        def session_transaction(self, *args, **kwargs):
            return self._client.session_transaction(*args, **kwargs)

    return _SecureClient(client)


# ---------------------------------------------------------------------------
# Work objects
# ---------------------------------------------------------------------------

@pytest.fixture()
def project(ceo, member):
    """A project led by the CEO with one member."""
    from app.services.project_service import create_project

    return create_project(
        actor=ceo,
        name="Raasta",
        description="Test project",
        status="ACTIVE",
        owner=ceo,
        members=[member],
    )


@pytest.fixture()
def task(ceo, member, project):
    """An assigned task, ready to be worked on."""
    from app.services.task_service import create_task

    return create_task(
        actor=ceo,
        title="Implement the routing table",
        project=project,
        assignee=member,
        reviewer=ceo,
        description="Build the routing table and cover it with tests.",
        acceptance_criteria="Routes resolve correctly and the test suite passes.",
        priority="HIGH",
        difficulty="MEDIUM",
    )


@pytest.fixture()
def submission(ceo, member, task):
    """A submission awaiting review, created through the service."""
    from app.services.task_service import submit_work

    return submit_work(
        task,
        actor=member,
        summary="Implemented the routing table with full test coverage.",
        external_link="https://example.com/pull/1",
    )


@pytest.fixture()
def focus_session(member):
    """A completed, credited focus session for today."""
    from datetime import timedelta

    from app.models.base import utcnow
    from app.models.focus import FocusSession, FocusStatus
    from app.utils.dates import workday_for

    now = utcnow()
    row = FocusSession(
        user_id=member.id,
        goal="Finish the routing table",
        workday_date=workday_for(now),
        status=FocusStatus.COMPLETED.value,
        started_at=now - timedelta(minutes=25),
        last_resumed_at=now - timedelta(minutes=25),
        accumulated_seconds=1500,
        ended_at=now,
        duration_seconds=1500,
        credited=True,
    )
    _db.session.add(row)
    _db.session.commit()
    return row
