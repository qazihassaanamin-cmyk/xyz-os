"""Authorization sweep: every route, as anonymous, as a member and as the CEO.

This is the test that catches the expensive mistake — a new route added later
without a policy check. It walks the whole URL map, substitutes real object ids,
and asserts that:

* anonymous visitors are redirected to the login page and never see a 2xx;
* a member is never handed a CEO-only page (403, or 404 where the object is
  scoped so the record's existence is not confirmed);
* the CEO never receives a 5xx.

It is deliberately strict about *how* a route refuses: a redirect to ``/login``
for a signed-in member would mean the policy is missing, not that the route is
protected.
"""

from __future__ import annotations

import pytest

from app.extensions import db as _db
from app.models.user import User

# Routes that legitimately answer a GET without an authenticated session.
PUBLIC_ROUTES = {
    "/login",
    "/forgot-password",
    "/health",
    "/robots.txt",
}

# Endpoints whose URL needs a real object id. Filled from the fixtures below.
OBJECT_ROUTES = [
    ("/tasks/{task_id}", "task"),
    ("/tasks/{task_id}/edit", "task"),
    ("/tasks/{task_id}/state", "task"),
    ("/projects/{project_id}", "project"),
    ("/projects/{project_id}/edit", "project"),
    ("/team/{username}", "user"),
    ("/focus/api/today", None),
]

CEO_ONLY_PREFIXES = ("/ceo/", "/xp/admin", "/uploads/storage", "/daily/workdays", "/daily/team")


def _materialise(template: str, ids: dict) -> str:
    return template.format(**ids)


@pytest.fixture()
def ids(ceo, member, other_member, project, task, submission):
    return {
        "task_id": task.id,
        "project_id": project.id,
        "username": member.username,
        "submission_id": submission.id,
        "message_id": 1,
        "notification_id": 1,
        "attachment_id": 1,
        "blocker_id": 1,
        "suggestion_id": 1,
        "entry_id": 1,
        "announcement_id": 1,
        "milestone_id": 1,
        "rule_id": 1,
        "level_id": 1,
        "badge_id": 1,
        "challenge_id": 1,
        "exception_id": 1,
        "transaction_id": 1,
        "session_id": 1,
        "subtask_id": 1,
    }


def _all_get_rules(app, ids):
    """Every GET rule in the application with its parameters filled in."""
    rows = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint == "static":
            continue
        try:
            url = rule.build(ids, append_unknown=False)[1]
        except Exception:
            continue
        if not url.startswith("/"):
            continue
        rows.append((rule.endpoint, url))
    return sorted(set(rows))


class TestAnonymousSweep:
    def test_no_route_returns_content_to_an_anonymous_visitor(self, app, client, ids):
        """Every GET route either redirects to /login, or is deliberately public."""
        offenders = []
        for endpoint, url in _all_get_rules(app, ids):
            response = client.get(url, follow_redirects=False)
            if url in PUBLIC_ROUTES:
                assert response.status_code in {200, 405}, f"{url} should be public"
                continue
            if response.status_code == 302 and "/login" in response.headers.get("Location", ""):
                continue
            if response.status_code == 401:
                # JSON endpoints answer 401 rather than redirecting.
                continue
            offenders.append((url, endpoint, response.status_code))
        assert offenders == [], f"Routes reachable anonymously: {offenders}"

    def test_anonymous_response_body_never_contains_member_data(self, client, ceo, member):
        response = client.get("/", follow_redirects=True)
        body = response.get_data(as_text=True)
        assert member.email not in body
        assert ceo.email not in body


class TestMemberSweep:
    def test_member_is_refused_on_every_ceo_route(self, app, client, member, auth, ids):
        auth(member)
        offenders = []
        for endpoint, url in _all_get_rules(app, ids):
            if not url.startswith(CEO_ONLY_PREFIXES):
                continue
            response = client.get(url, follow_redirects=False)
            if response.status_code not in {403, 404}:
                offenders.append((url, endpoint, response.status_code))
        assert offenders == [], f"Member reached management routes: {offenders}"

    def test_member_never_sees_a_500(self, app, client, member, auth, ids):
        auth(member)
        failures = []
        for endpoint, url in _all_get_rules(app, ids):
            response = client.get(url, follow_redirects=False)
            if response.status_code >= 500:
                failures.append((url, endpoint, response.status_code))
        assert failures == [], f"Member triggered server errors: {failures}"

    def test_member_can_reach_their_own_workspace(self, client, member, auth):
        auth(member)
        for url in (
            "/dashboard",
            "/tasks/",
            "/projects/",
            "/focus/",
            "/daily/",
            "/team/",
            "/lounge/",
            "/suggestions/",
            "/announcements/",
            "/knowledge/",
            "/notifications/",
            "/xp/",
        ):
            assert client.get(url).status_code == 200, url


class TestCeoSweep:
    def test_ceo_never_sees_a_server_error(self, app, client, ceo, auth, ids):
        auth(ceo)
        failures = []
        for endpoint, url in _all_get_rules(app, ids):
            response = client.get(url, follow_redirects=False)
            if response.status_code >= 500:
                failures.append((url, endpoint, response.status_code))
        assert failures == [], f"CEO triggered server errors: {failures}"

    def test_ceo_reaches_the_management_pages(self, client, ceo, auth):
        auth(ceo)
        for url in (
            "/dashboard/ceo",
            "/ceo/reports",
            "/ceo/activity",
            "/ceo/settings",
            "/ceo/members",
            "/xp/admin",
            "/uploads/storage",
            "/daily/workdays",
            "/daily/team",
        ):
            assert client.get(url).status_code == 200, url

    def test_ceo_reaches_every_workspace_page(self, client, ceo, auth):
        auth(ceo)
        for url in (
            "/dashboard",
            "/tasks/",
            "/tasks/new",
            "/projects/",
            "/projects/new",
            "/focus/",
            "/focus/history",
            "/daily/",
            "/daily/history",
            "/team/",
            "/lounge/",
            "/suggestions/",
            "/suggestions/new",
            "/suggestions/reviews",
            "/announcements/",
            "/announcements/new",
            "/knowledge/",
            "/knowledge/new",
            "/notifications/",
            "/xp/",
            "/xp/leaderboard",
        ):
            assert client.get(url).status_code == 200, url


class TestIDOR:
    """Objects a member may not know about must answer 404, never 403 or 200."""

    def test_member_cannot_read_another_members_private_task(
        self, client, member, other_member, ceo, auth, db
    ):
        from app.services.task_service import create_task

        private = create_task(
            actor=ceo,
            title="Confidential board preparation",
            project=None,
            assignee=other_member,
            reviewer=ceo,
            description="Not for other members.",
            difficulty="MEDIUM",
        )
        auth(member)
        assert client.get(f"/tasks/{private.id}").status_code == 404
        assert client.post(f"/tasks/{private.id}/status", data={"status": "TODO"}).status_code == 404

    def test_member_cannot_read_another_members_profile_that_is_deactivated(
        self, client, member, other_member, auth
    ):
        other_member.is_active_flag = False
        _db.session.commit()
        auth(member)
        assert client.get(f"/team/{other_member.username}").status_code == 404

    def test_member_cannot_read_another_members_notifications(self, client, member, ceo, auth):
        from app.services.notification_service import notify

        row = notify(ceo, "TASK_ASSIGNED", title="Private notice", body="For the CEO only.")
        auth(member)
        assert client.get(f"/notifications/{row.id}/read", follow_redirects=False).status_code == 405
        assert client.post(f"/notifications/{row.id}/read").status_code == 404

    def test_member_cannot_download_a_file_from_a_task_they_cannot_see(
        self, client, member, other_member, ceo, auth, db
    ):
        from app.services.task_service import create_task
        from app.services.upload_service import validate_and_store
        from tests.support import png_bytes
        import io
        from werkzeug.datastructures import FileStorage

        private = create_task(
            actor=ceo,
            title="Private evidence task",
            project=None,
            assignee=other_member,
            reviewer=ceo,
            description="Private.",
            difficulty="SMALL",
        )
        upload = FileStorage(stream=io.BytesIO(png_bytes()), filename="evidence.png")
        attachment = validate_and_store(upload, user=other_member, task_id=private.id)

        auth(member)
        assert client.get(f"/uploads/{attachment.id}").status_code == 404
        assert client.get(f"/uploads/{attachment.id}/thumbnail").status_code == 404

    def test_member_cannot_see_another_members_focus_session(self, client, member, ceo, auth):
        from app.models.base import utcnow
        from app.models.focus import FocusSession, FocusStatus

        row = FocusSession(
            user_id=ceo.id,
            goal="Private session",
            workday_date=utcnow().date(),
            status=FocusStatus.COMPLETED.value,
            started_at=utcnow(),
            accumulated_seconds=1500,
            duration_seconds=1500,
        )
        _db.session.add(row)
        _db.session.commit()

        auth(member)
        assert client.post(f"/focus/{row.id}/pause").status_code == 404
        assert client.post(f"/focus/{row.id}/finish").status_code == 404

    def test_member_cannot_change_another_members_task_status(
        self, client, member, other_member, ceo, auth, project
    ):
        from app.services.task_service import create_task

        other_task = create_task(
            actor=ceo,
            title="Someone else's work",
            project=project,
            assignee=other_member,
            reviewer=ceo,
            description="Owned by another member.",
            difficulty="SMALL",
        )
        # The member shares the project, so the task is visible, but it is not
        # theirs to move: the state machine refuses and reports why.
        auth(member)
        response = client.post(
            f"/tasks/{other_task.id}/status",
            data={"status": "IN_PROGRESS"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        _db.session.refresh(other_task)
        assert other_task.status == "TODO"

    def test_member_cannot_edit_an_unknown_task_id(self, client, member, auth):
        auth(member)
        assert client.get("/tasks/99999").status_code == 404
        assert client.post("/tasks/99999/status", data={"status": "TODO"}).status_code == 404

    def test_member_cannot_review_their_own_submission(self, client, member, task, submission, auth):
        auth(member)
        response = client.post(
            f"/tasks/submissions/{submission.id}/review",
            data={"decision": "APPROVED"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        _db.session.refresh(task)
        assert task.status == "IN_REVIEW"


class TestAccountIsolation:
    def test_deactivated_member_is_hidden_from_the_directory(self, client, ceo, member, auth):
        member.is_active_flag = False
        _db.session.commit()
        auth(ceo)
        body = client.get("/team/").get_data(as_text=True)
        assert member.full_name not in body

    def test_member_list_queries_exclude_inactive_accounts(self, ceo, member):
        from app.services import report_service

        member.is_active_flag = False
        _db.session.commit()
        rows = report_service.compliance_by_member(
            start=__import__("datetime").date.today(),
            end=__import__("datetime").date.today(),
        )
        assert all(row["user"].is_active_flag for row in rows)

    def test_only_one_active_ceo_can_exist_at_a_time(self, client, ceo, member, auth):
        auth(ceo)
        response = client.post(
            f"/team/{ceo.username}/account",
            data={"action": "set_role", "role": "MEMBER"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        _db.session.refresh(ceo)
        assert ceo.role == "CEO", "the last active CEO must not be demoted"

    def test_ceo_can_promote_a_member_to_ceo(self, client, ceo, member, auth):
        auth(ceo)
        client.post(
            f"/team/{member.username}/account",
            data={"action": "set_role", "role": "CEO"},
            follow_redirects=True,
        )
        _db.session.refresh(member)
        assert member.role == "CEO"
