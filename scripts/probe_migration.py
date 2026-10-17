"""Diagnostic: does the Alembic chain land in the app's own database?"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_ENV"] = "testing"
os.environ.setdefault("SECRET_KEY", "testing-secret-key-not-used-in-production")

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app import create_app
from app.extensions import db

directory = tempfile.mkdtemp(prefix="probe-")
database = os.path.join(directory, "probe.sqlite")
os.environ["DATABASE_URL"] = f"sqlite:///{database}"

app = create_app("testing")
print("app uri     :", app.config["SQLALCHEMY_DATABASE_URI"])

with app.app_context():
    print("engine url  :", db.engine.url)
    config = Config("migrations/alembic.ini")
    config.set_main_option("script_location", "migrations")
    command.upgrade(config, "head")
    print("tables      :", len(inspect(db.engine).get_table_names()))

    import sqlite3

    con = sqlite3.connect(database)
    print("file tables :", len(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
