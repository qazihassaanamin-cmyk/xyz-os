"""Smoke-test every GET route as the CEO and as a member.

Uses the development database and the real application. Any 500, or any
template error, is reported with the traceback so it can be fixed rather than
discovered in production.
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

# Routes that take no parameters, plus the ones we can construct from the data.
STATIC_GETS = [
    "/",
    "/dashboard",
    "/dashboard/ceo",
    "/dashboard/attention",
    "/dashboard/attention?kind=review",
    "/dashboard/attention?kind=blockers",
    "/dashboard/attention?kind=checkins",
    "/dashboard/attention?kind=milestones",
    "/tasks/",
    "/tasks/?view=review",
    "/tasks/new",
    "/tasks/blockers",
    "/tasks/reviews",
    "/projects/",
    "/projects/new",
    "/focus/",
    "/focus/history",
    "/focus/state",
    "/focus/api/today",
    "/daily/",
    "/daily/history",
    "/daily/workdays",
    "/daily/team",
    "/team/",
    "/notifications/",
    "/notifications/recent",
    "/uploads/storage",
    "/suggestions/",
    "/suggestions/new",
    "/suggestions/reviews",
    "/lounge/",
    "/lounge/?topic=tech",
    "/lounge/api/messages",
    "/announcements/",
    "/announcements/new",
    "/knowledge/",
    "/knowledge/new",
    "/xp/",
    "/xp/leaderboard",
    "/xp/admin",
    "/ceo/reports",
    "/ceo/activity",
    "/ceo/settings",
    "/ceo/members",
    "/login",
    "/forgot-password",
    "/robots.txt",
    "/health",
]


def run() -> int:
    failures: list[str] = []
    checked = 0

    with app.app_context():
        ceo = User.query.filter_by(role="CEO").first()
        member = User.query.filter_by(role="MEMBER").first()
        if ceo is None or member is None:
            print("No seeded users found. Run `flask setup` first.")
            return 1

        extra = []
        from app.models.project import Project
        from app.models.suggestion import Suggestion
        from app.models.task import Task

        project = Project.query.first()
        task = Task.query.first()
        suggestion = Suggestion.query.first()
        if project:
            extra.append(f"/projects/{project.id}")
            extra.append(f"/projects/{project.id}/edit")
        if task:
            extra.append(f"/tasks/{task.id}")
            extra.append(f"/tasks/{task.id}/edit")
            extra.append(f"/tasks/{task.id}/state")
        if suggestion:
            extra.append(f"/suggestions/{suggestion.id}")
        extra.append(f"/team/{ceo.username}")
        extra.append(f"/team/{member.username}")
        extra.append(f"/team/{member.username}?tab=work")
        extra.append(f"/team/{member.username}?tab=achievements")
        extra.append(f"/team/{member.username}?tab=recognition")
        extra.append(f"/team/{member.username}?tab=focus")
        extra.append(f"/team/{member.username}?tab=activity")
        extra.append(f"/xp/history/{member.username}")

        urls = STATIC_GETS + extra

        for user in (ceo, member):
            client = app.test_client()
            with client.session_transaction() as session:
                session["_user_id"] = str(user.id)
                session["_fresh"] = True

            for url in urls:
                checked += 1
                try:
                    response = client.get(url, follow_redirects=False)
                except Exception:
                    failures.append(
                        f"{user.username} GET {url} raised:\n"
                        + traceback.format_exc(limit=6)
                    )
                    continue

                if response.status_code >= 500:
                    body = response.get_data(as_text=True)
                    marker = ""
                    for line in body.splitlines():
                        if "Error" in line or "error" in line:
                            marker = line.strip()[:200]
                            break
                    failures.append(
                        f"{user.username} GET {url} -> {response.status_code} {marker}"
                    )
                elif response.status_code == 404 and url not in (
                    "/nonexistent",
                ) and not url.startswith("/team/") is False:
                    # A 404 on a route we constructed means the object lookup
                    # rejected a legitimate request; worth knowing about.
                    pass

    print(f"checked {checked} route/user combinations")
    if failures:
        print(f"\nFAILURES: {len(failures)}")
        for item in failures:
            print(" ", item)
        return 1

    print("all GET routes rendered without a server error")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
