"""Smoke-test every GET route in the application as each role.

Discovers routes from the URL map rather than a hand-written list, so a route
that is added later is covered automatically. Substitutes real object ids for
path parameters and reports anything that returns a 5xx or raises.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.user import User  # noqa: E402

app = create_app("development")

# Routes deliberately not exercised here, with the reason.
SKIP_ENDPOINTS = {
    "static",
    "auth.logout",                 # POST only
    "auth.reset_password",         # needs a live token
    "auth.accept_invite",          # needs a live invitation
    "auth.change_password",        # redirects when the flag is clear; harmless
    "uploads.download",            # needs a stored file
    "uploads.preview",
    "uploads.thumbnail",
}


def build_params(rule) -> dict | None:
    """Fill path parameters with real ids from the database."""
    from app.models.attachment import Attachment
    from app.models.project import Milestone, Project
    from app.models.suggestion import Suggestion
    from app.models.task import Blocker, Submission, Task
    from app.models.user import User as U

    params: dict = {}
    for argument in rule.arguments:
        name = argument.lower()
        if "task" in name:
            row = Task.query.first()
        elif "project" in name:
            row = Project.query.first()
        elif "suggestion" in name:
            row = Suggestion.query.first()
        elif "milestone" in name:
            row = Milestone.query.first()
        elif "submission" in name:
            row = Submission.query.first()
        elif "blocker" in name:
            row = Blocker.query.first()
        elif "attachment" in name or "file" in name:
            row = Attachment.query.first()
        elif "badge" in name:
            from app.models.achievement import MemberBadge

            row = MemberBadge.query.first()
        elif "level" in name:
            from app.models.xp import LevelDefinition

            row = LevelDefinition.query.first()
        elif "rule" in name:
            from app.models.xp import XpRule

            row = XpRule.query.first()
        elif "challenge" in name:
            from app.models.challenge import Challenge

            row = Challenge.query.first()
        elif "announcement" in name:
            from app.models.content import Announcement

            row = Announcement.query.first()
        elif "entry" in name or "doc" in name:
            from app.models.content import KnowledgeEntry

            row = KnowledgeEntry.query.first()
        elif "notification" in name:
            from app.models.notification import Notification

            row = Notification.query.first()
        elif "username" in name:
            row = U.query.filter_by(role="MEMBER").first()
        elif "user" in name:
            row = U.query.filter_by(role="MEMBER").first()
        elif "token" in name:
            return None  # needs a real token
        else:
            return None

        if row is None:
            return None
        value = getattr(row, "username", None) if "username" in name else None
        params[argument] = value if value else row.id
    return params


def run() -> int:
    failures: list[str] = []
    checked = 0

    with app.app_context():
        from app.models.user import User as U

        ceo = U.query.filter_by(role="CEO").first()
        member = U.query.filter_by(role="MEMBER").first()
        if ceo is None or member is None:
            print("No seeded users. Run `flask setup` first.")
            return 1

        targets = [("ceo", ceo), ("member", member)]

        rules = [
            rule
            for rule in app.url_map.iter_rules()
            if "GET" in rule.methods and rule.endpoint not in SKIP_ENDPOINTS
        ]

        for label, user in targets:
            client = app.test_client()
            with client.session_transaction() as session:
                session["_user_id"] = str(user.id)
                session["_fresh"] = True

            for rule in sorted(rules, key=lambda r: str(r)):
                params = build_params(rule)
                if params is None:
                    continue
                try:
                    url = rule.build(params, append_unknown=False)[1]
                except Exception:
                    continue

                checked += 1
                try:
                    response = client.get(url, follow_redirects=False)
                except Exception:
                    failures.append(
                        f"{label} GET {url} raised:\n" + traceback.format_exc(limit=5)
                    )
                    continue

                if response.status_code >= 500:
                    failures.append(f"{label} GET {url} -> {response.status_code}")

    print(f"checked {checked} route/role combinations across {len(app.url_map._rules)} rules")
    if failures:
        print(f"\nFAILURES: {len(failures)}")
        for item in failures:
            print(" ", item)
        return 1

    print("every reachable GET route rendered without a server error")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
