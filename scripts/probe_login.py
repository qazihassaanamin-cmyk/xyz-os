"""Diagnostic: reproduce the login 400 with a valid CSRF token."""
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_ENV"] = "testing"
os.environ.setdefault("SECRET_KEY", "testing-secret-key-not-used-in-production")

logging.basicConfig(level=logging.DEBUG)

from app import create_app  # noqa: E402

app = create_app("testing")
app.config["SERVER_NAME"] = "localhost"
app.config["WTF_CSRF_ENABLED"] = True
client = app.test_client()

# The probe uses the application's own development database; create the schema
# so a missing table does not mask the CSRF behaviour being investigated.
with app.app_context():
    from app.extensions import db as _db

    _db.create_all()

page = client.get("/login")
print("GET /login ->", page.status_code)
match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True))
print("token in HTML:", bool(match))
with client.session_transaction() as session:
    print("csrf_token in session:", repr(session.get("csrf_token"))[:90])

response = client.post(
    "/login",
    data={"identifier": "someone", "password": "some-password", "csrf_token": match.group(1) if match else ""},
)
print("POST /login ->", response.status_code)
body = response.get_data(as_text=True)
print(body[:400] if body else "(empty body)")
