"""Minimal probe: does creating the app, the database and one user work?

Run this directly. If it prints "OK", the application layer is fine and the hang
is in pytest's own machinery. If it hangs, the fault is here and this script
shows exactly which line.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["FLASK_ENV"] = "testing"
os.environ["SECRET_KEY"] = "probe-secret-key"
os.environ["ANON_VOTE_SALT"] = "probe-salt"


def step(label):
    print(f"[{time.strftime('%H:%M:%S')}] {label}", flush=True)


step("importing app")
from app import create_app
from app.extensions import db

step("creating app")
with tempfile.TemporaryDirectory(prefix="probe-") as directory:
    db_path = os.path.join(directory, "probe.sqlite")
    uploads = os.path.join(directory, "uploads")
    os.makedirs(uploads, exist_ok=True)

    app = create_app("testing")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        UPLOAD_DIR=uploads,
        UPLOAD_DIR_ABS=uploads,
        RATELIMIT_ENABLED=False,
        TESTING=True,
        SERVER_NAME="localhost",
    )

    step("pushing context")
    with app.app_context():
        step("disposing engine")
        db.session.remove()
        db.get_engine().dispose()

        step("create_all")
        db.create_all()

        step("seeding settings")
        from app.services import settings_service

        settings_service.ensure_defaults()

        step("seeding levels")
        from app.services.level_service import ensure_ladder

        ensure_ladder()

        step("seeding xp rules")
        from app.services.xp_service import ensure_rules

        ensure_rules()

        step("seeding achievements")
        from app.services.achievement_service import ensure_catalogue

        ensure_catalogue()

        step("creating a user")
        from app.models.user import User
        from app.services.xp_service import ensure_profile

        user = User(
            username="probe",
            email="probe@tavryc.com",
            full_name="Probe User",
            role="CEO",
        )
        user.set_password("probe-password-1234")
        db.session.add(user)
        db.session.commit()
        step("user committed")

        ensure_profile(user)
        step("profile ensured")

        step("querying back")
        assert User.query.count() == 1

        step("creating a client and fetching /login")
        client = app.test_client()
        response = client.get("/login")
        step(f"GET /login -> {response.status_code}")

        step("posting a login")
        with client.session_transaction() as session:
            token = session.get("csrf_token")
        response = client.post(
            "/login",
            data={
                "identifier": "probe",
                "password": "probe-password-1234",
                "csrf_token": token or "",
            },
        )
        step(f"POST /login -> {response.status_code}")

step("OK — the application layer works")

# Now check whether pytest itself can start.
step("importing pytest")
import pytest

step(f"pytest version {pytest.__version__}")

step("checking plugins")
import importlib

for name in ("flask", "timeout", "cacheprovider"):
    try:
        importlib.import_module(f"_pytest.{name}")
        step(f"  _pytest.{name} importable")
    except ImportError:
        step(f"  _pytest.{name} not present")

step("probe complete")
