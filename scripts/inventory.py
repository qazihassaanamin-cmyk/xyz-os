"""Print every table (with columns) and every route. Developer inspection aid."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_ENV", "testing")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402

app = create_app("testing")
with app.app_context():
    if "--tables" in sys.argv:
        for name, table in sorted(db.metadata.tables.items()):
            print(name, ":", ", ".join(c.name for c in table.columns))
    if "--routes" in sys.argv:
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
            methods = ",".join(sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}))
            print(f"{methods:12} {rule.rule:60} {rule.endpoint}")
