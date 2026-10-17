"""Authentication: login, logout, redirects, lockout and account state.

Form posts use the ``secure_client`` fixture, which supplies a real CSRF token
the way a browser does. Tests that assert CSRF rejection use the plain client.
"""

from __future__ import annotations

import pytest

from app.extensions import db as _db
from app.models.login_attempt import LoginAttempt
from app.models.user import User

PASSWORD = "test-password-1234"


class TestLogin:
    def test_valid_login_by_username(self, secure_client, ceo):
        response = secure_client.post(
            "/login",
            data={"identifier": "ceo", "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/dashboard/ceo" in response.headers["Location"]

    def test_valid_login_by_email(self, secure_client, member):
        response = secure_client.post(
            "/login",
            data={"identifier": "saad@tavryc.com", "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_wrong_password_is_rejected_generically(self, secure_client, ceo):
        response = secure_client.post(
            "/login", data={"identifier": "ceo", "password": "definitely-not-it"}
        )
        assert response.status_code == 401
        body = response.get_data(as_text=True)
        assert "Incorrect username or password" in body
        assert "no such user" not in body.lower()
        assert "not found" not in body.lower()

    def test_unknown_user_gives_the_same_message(self, secure_client, ceo):
        known = secure_client.post(
            "/login", data={"identifier": "ceo", "password": "wrong-password-here"}
        ).get_data(as_text=True)
        unknown = secure_client.post(
            "/login",
            data={"identifier": "nobody@tavryc.com", "password": "wrong-password-here"},
        ).get_data(as_text=True)
        assert "Incorrect username or password" in known
        assert "Incorrect username or password" in unknown

    def test_failed_attempt_is_recorded(self, secure_client, ceo):
        secure_client.post("/login", data={"identifier": "ceo", "password": "wrong"})
        attempt = LoginAttempt.query.order_by(LoginAttempt.id.desc()).first()
        assert attempt is not None
        assert attempt.success is False
        assert attempt.reason in {"bad_password", "unknown_user"}

    def test_deactivated_user_cannot_sign_in(self, secure_client, make_user):
        make_user(username="gone", role="MEMBER", is_active=False)
        response = secure_client.post(
            "/login", data={"identifier": "gone", "password": PASSWORD}
        )
        assert response.status_code == 401
        assert "not active" in response.get_data(as_text=True).lower()

    def test_lockout_after_repeated_failures(self, secure_client, ceo):
        for _ in range(6):
            secure_client.post("/login", data={"identifier": "ceo", "password": "wrong"})

        response = secure_client.post(
            "/login", data={"identifier": "ceo", "password": PASSWORD}
        )
        # Even the correct password is refused while the account is locked.
        assert response.status_code == 401
        assert "temporarily locked" in response.get_data(as_text=True).lower()

    def test_login_page_is_the_first_thing_an_anonymous_visitor_sees(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_login_page_renders_with_the_company_name(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Sign in" in body
        assert "Tavryc" in body

    def test_login_form_carries_a_csrf_token(self, client):
        assert 'name="csrf_token"' in client.get("/login").get_data(as_text=True)

    def test_login_without_csrf_is_refused(self, client, ceo):
        response = client.post("/login", data={"identifier": "ceo", "password": PASSWORD})
        assert response.status_code == 400


class TestSession:
    @pytest.mark.parametrize(
        "path",
        [
            "/dashboard",
            "/dashboard/ceo",
            "/dashboard/attention",
            "/tasks/",
            "/tasks/new",
            "/tasks/blockers",
            "/tasks/reviews",
            "/projects/",
            "/projects/new",
            "/focus/",
            "/focus/history",
            "/daily/",
            "/daily/history",
            "/daily/workdays",
            "/daily/team",
            "/team/",
            "/team/ceo",
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
            "/xp/admin",
            "/ceo/reports",
            "/ceo/activity",
            "/ceo/settings",
            "/ceo/members",
            "/uploads/storage",
        ],
    )
    def test_protected_route_redirects_when_anonymous(self, client, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_anonymous_json_request_gets_401_not_a_redirect(self, client):
        response = client.get("/focus/state", headers={"Accept": "application/json"})
        assert response.status_code == 401
        assert response.get_json()["error"] == "authentication_required"

    def test_anonymous_lounge_poll_gets_401(self, client):
        # The lounge poll endpoint is a JSON API, so a poller sends
        # ``Accept: application/json``; without it the global gate redirects to
        # the login page instead.
        response = client.get(
            "/lounge/api/messages", headers={"Accept": "application/json"}
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "authentication_required"

    def test_authenticated_user_reaches_the_dashboard(self, client, member, auth):
        auth(member)
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "My dashboard" in response.get_data(as_text=True)

    def test_logout_requires_post(self, client, member, auth):
        auth(member)
        assert client.get("/logout", follow_redirects=False).status_code == 405

    def test_logout_without_csrf_is_refused(self, client, member, auth):
        auth(member)
        assert client.post("/logout", follow_redirects=False).status_code == 400

    def test_logout_with_csrf_clears_the_session(self, secure_client, member, auth):
        auth(member)
        response = secure_client.post("/logout", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        assert secure_client.get("/dashboard", follow_redirects=False).status_code == 302

    def test_deactivated_user_is_rejected_on_the_next_request(self, client, member, auth):
        auth(member)
        member.is_active_flag = False
        _db.session.commit()
        response = client.get("/dashboard", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_session_cookie_is_httponly_and_samesite(self, secure_client, member):
        response = secure_client.post(
            "/login", data={"identifier": "saad", "password": PASSWORD}
        )
        cookie = response.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie.replace("samesite", "SameSite")

    def test_security_headers_are_present(self, client):
        response = client.get("/login")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_robots_disallows_indexing(self, client):
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert "Disallow: /" in response.get_data(as_text=True)

    def test_health_endpoint_is_public_and_minimal(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert set(response.get_json()) == {"status", "database"}


class TestRedirectSafety:
    """``?next=`` must never send anyone off-site."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example.com/steal",
            "//evil.example.com/steal",
            "http://evil.example.com",
            "/\\evil.example.com",
            "\\\\evil.example.com",
            "javascript:alert(1)",
        ],
    )
    def test_hostile_next_is_ignored(self, secure_client, member, hostile):
        response = secure_client.post(
            "/login",
            data={"identifier": "saad", "password": PASSWORD, "next": hostile},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "evil.example.com" not in location
        assert location.startswith("/")

    def test_safe_relative_next_is_honoured(self, secure_client, member):
        response = secure_client.post(
            "/login",
            data={"identifier": "saad", "password": PASSWORD, "next": "/tasks/"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/tasks/")

    def test_safe_redirect_helper_rejects_schemes(self):
        from app.utils.redirects import is_safe_redirect_target

        assert is_safe_redirect_target("/tasks/") is True
        assert is_safe_redirect_target("https://evil.example.com") is False
        assert is_safe_redirect_target("//evil.example.com") is False
        assert is_safe_redirect_target("/\\evil.example.com") is False
        assert is_safe_redirect_target(None) is False


class TestRoleRedirects:
    def test_ceo_lands_on_the_command_centre(self, secure_client, ceo):
        response = secure_client.post(
            "/login",
            data={"identifier": "ceo", "password": PASSWORD},
            follow_redirects=False,
        )
        assert "/dashboard/ceo" in response.headers["Location"]

    def test_plain_member_lands_on_their_dashboard(self, secure_client, member):
        response = secure_client.post(
            "/login",
            data={"identifier": "saad", "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.headers["Location"].endswith("/dashboard")

    def test_project_lead_lands_on_projects(self, secure_client, ceo, member, project):
        from app.models.project import ProjectMember, ProjectRole

        membership = ProjectMember.query.filter_by(
            project_id=project.id, user_id=member.id
        ).first()
        membership.role_in_project = ProjectRole.LEAD.value
        _db.session.commit()

        response = secure_client.post(
            "/login",
            data={"identifier": "saad", "password": PASSWORD},
            follow_redirects=False,
        )
        assert "/projects" in response.headers["Location"]

    def test_remember_me_sets_a_remember_cookie(self, secure_client, member):
        response = secure_client.post(
            "/login",
            data={"identifier": "saad", "password": PASSWORD, "remember": "y"},
            follow_redirects=False,
        )
        assert "remember_token" in response.headers.get("Set-Cookie", "")


class TestPasswordReset:
    def test_request_does_not_reveal_whether_an_account_exists(self, secure_client):
        known = secure_client.post(
            "/forgot-password", data={"email": "saad@tavryc.com"}, follow_redirects=True
        ).get_data(as_text=True)
        unknown = secure_client.post(
            "/forgot-password", data={"email": "nobody@tavryc.com"}, follow_redirects=True
        ).get_data(as_text=True)
        assert "If that email address belongs to a member" in known
        assert "If that email address belongs to a member" in unknown

    def test_expired_token_is_unusable(self, member):
        from datetime import timedelta

        from app.models.base import utcnow
        from app.services import auth_service

        user = User.query.filter_by(id=member.id).first()
        raw = auth_service.create_reset_token(user)
        token_row = auth_service.get_reset_token(raw)
        assert token_row.is_usable

        token_row.expires_at = utcnow() - timedelta(minutes=1)
        _db.session.commit()
        assert auth_service.get_reset_token(raw).is_usable is False

    def test_completed_reset_cannot_be_reused(self, member):
        from app.services import auth_service

        user = User.query.filter_by(id=member.id).first()
        raw = auth_service.create_reset_token(user)
        auth_service.complete_password_reset(raw, "a-brand-new-password-99")

        with pytest.raises(auth_service.AuthError):
            auth_service.complete_password_reset(raw, "another-password-12345")

    def test_reset_token_is_stored_hashed_not_plain(self, member):
        from app.services import auth_service

        user = User.query.filter_by(id=member.id).first()
        raw = auth_service.create_reset_token(user)
        token_row = auth_service.get_reset_token(raw)
        assert raw not in token_row.token_hash
        assert len(token_row.token_hash) == 64

    def test_new_token_invalidates_the_previous_one(self, member):
        from app.services import auth_service

        user = User.query.filter_by(id=member.id).first()
        first = auth_service.create_reset_token(user)
        second = auth_service.create_reset_token(user)
        assert auth_service.get_reset_token(first).is_usable is False
        assert auth_service.get_reset_token(second).is_usable is True

    def test_reset_route_rejects_an_unknown_token(self, client):
        assert client.get("/reset-password/not-a-real-token").status_code == 410


class TestInvitations:
    def test_invitation_token_is_hashed_and_single_use(self, ceo):
        from app.models.user import Invitation
        from app.services import auth_service

        actor = User.query.filter_by(id=ceo.id).first()
        invitation, raw = auth_service.create_invitation(
            "newfounder@tavryc.com", "MEMBER", actor
        )
        assert raw not in invitation.token_hash
        assert invitation.is_usable

        auth_service.accept_invitation(
            raw,
            username="newfounder",
            full_name="New Founder",
            password="a-strong-password-2024",
        )
        assert Invitation.query.filter_by(id=invitation.id).first().is_usable is False

    def test_invitation_cannot_be_used_twice(self, ceo):
        from app.services import auth_service

        actor = User.query.filter_by(id=ceo.id).first()
        _, raw = auth_service.create_invitation("second@tavryc.com", "MEMBER", actor)
        auth_service.accept_invitation(
            raw,
            username="second",
            full_name="Second Person",
            password="a-strong-password-2024",
        )
        with pytest.raises(auth_service.AuthError):
            auth_service.accept_invitation(
                raw,
                username="third",
                full_name="Third Person",
                password="a-strong-password-2024",
            )

    def test_invitation_does_not_create_a_duplicate_account(self, ceo, member):
        from app.models.user import User as U
        from app.services import auth_service

        actor = U.query.filter_by(id=ceo.id).first()
        before = U.query.count()
        with pytest.raises(ValueError):
            auth_service.create_invitation("saad@tavryc.com", "MEMBER", actor)
        assert U.query.count() == before

    def test_accepting_an_invitation_signs_the_member_in(self, secure_client, ceo):
        from app.models.user import User as U
        from app.services import auth_service

        actor = U.query.filter_by(id=ceo.id).first()
        _, raw = auth_service.create_invitation("fresh@tavryc.com", "MEMBER", actor)

        response = secure_client.post(
            f"/invite/{raw}",
            data={
                "username": "fresh",
                "full_name": "Fresh Member",
                "password": "a-strong-password-2024",
                "confirm": "a-strong-password-2024",
                "timezone": "Asia/Karachi",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/dashboard" in response.headers["Location"]
        assert U.query.filter_by(username="fresh").first() is not None

    def test_invalid_invitation_token_is_refused(self, client):
        assert client.get("/invite/nonsense").status_code == 410


class TestPasswordStorage:
    def test_passwords_are_hashed_with_argon2(self, member):
        user = User.query.filter_by(id=member.id).first()
        assert user.password_hash.startswith("$argon2")
        assert PASSWORD not in user.password_hash

    @pytest.mark.parametrize(
        "weak", ["short1", "password", "12345678", "abcdefghijkl"]
    )
    def test_weak_passwords_are_refused(self, weak):
        from app.services.crypto_service import PasswordError, validate_password_strength

        with pytest.raises(PasswordError):
            validate_password_strength(weak)

    def test_strong_password_is_accepted(self):
        from app.services.crypto_service import validate_password_strength

        validate_password_strength("a-perfectly-fine-passphrase-42")

    def test_password_containing_the_username_is_refused(self, member):
        from app.services.crypto_service import PasswordError, validate_password_strength

        with pytest.raises(PasswordError):
            validate_password_strength("saad-is-my-password-1", user=member)

    def test_change_password_requires_the_current_one(self, secure_client, member, auth):
        auth(member)
        response = secure_client.post(
            "/change-password",
            data={
                "current_password": "not-the-current-one",
                "password": "a-brand-new-password-77",
                "confirm": "a-brand-new-password-77",
            },
        )
        assert response.status_code == 200
        assert "not correct" in response.get_data(as_text=True).lower()

    def test_change_password_updates_the_hash(self, secure_client, member, auth):
        auth(member)
        secure_client.post(
            "/change-password",
            data={
                "current_password": PASSWORD,
                "password": "a-brand-new-password-77",
                "confirm": "a-brand-new-password-77",
            },
        )
        updated = User.query.filter_by(id=member.id).first()
        assert updated.check_password("a-brand-new-password-77")
