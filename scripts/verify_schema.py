"""Verify the migrated schema against the model metadata.

Run after ``flask db upgrade`` to confirm the database really contains every
table, the two partial unique indexes, and that the metadata matches.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402

app = create_app("development")

failures: list[str] = []

with app.app_context():
    expected = set(db.metadata.tables.keys())

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    path = uri.replace("sqlite:///", "", 1)
    if not path.startswith(("/", "\\")):
        import os

        path = os.path.abspath(os.path.join(app.root_path, "..", path))

    conn = sqlite3.connect(path)
    actual = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    actual.discard("alembic_version")

    missing = expected - actual
    extra = actual - expected

    print(f"expected tables : {len(expected)}")
    print(f"actual tables   : {len(actual)}")
    if missing:
        failures.append(f"missing tables: {sorted(missing)}")
    if extra:
        failures.append(f"unexpected tables: {sorted(extra)}")

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_focus%'"
        )
    }
    print(f"focus unique indexes: {sorted(indexes)}")
    for required in ("uq_focus_open_per_user", "uq_focus_credited_per_user_day"):
        if required not in indexes:
            failures.append(f"missing index: {required}")

    version = list(conn.execute("SELECT version_num FROM alembic_version"))
    print(f"alembic version : {version[0][0] if version else 'none'}")

    # Every foreign key must resolve to a real table.
    broken_fks = []
    for table in sorted(actual):
        for row in conn.execute(f"PRAGMA foreign_key_list('{table}')"):
            target = row[2]
            if target not in actual:
                broken_fks.append(f"{table} -> {target}")
    if broken_fks:
        failures.append(f"unresolved foreign keys: {broken_fks}")
    else:
        print("foreign keys    : all resolve")

if failures:
    print("\nFAILURES:")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)

print("\nSchema verification passed.")
