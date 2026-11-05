from datetime import UTC, datetime

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


def test_workspace_lifecycle_requires_admin_and_preserves_active_workspace():
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        onboarded = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Lifecycle Org",
                "owner_email": "owner@lifecycle.test",
                "owner_display_name": "Owner",
            },
        )
        assert onboarded.status_code == 201, onboarded.text
        owner_headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}
        default_workspace_id = onboarded.json()["workspace"]["id"]

        last_workspace_archive = client.patch(
            f"/api/v0/workspaces/{default_workspace_id}",
            headers=owner_headers,
            json={"status": "archived"},
        )
        assert last_workspace_archive.status_code == 422

        created = client.post(
            "/api/v0/workspaces",
            headers=owner_headers,
            json={"name": "Temporary", "environment": "test"},
        )
        assert created.status_code == 201, created.text
        temporary_id = created.json()["id"]

        archived = client.patch(
            f"/api/v0/workspaces/{temporary_id}",
            headers=owner_headers,
            json={"status": "archived"},
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"
        assert {
            item["id"]
            for item in client.get("/api/v0/workspaces", headers=owner_headers).json()
        } == {default_workspace_id}
        all_workspaces = client.get(
            "/api/v0/workspaces?include_archived=true", headers=owner_headers
        )
        assert all_workspaces.status_code == 200
        assert {item["id"] for item in all_workspaces.json()} == {
            default_workspace_id,
            temporary_id,
        }

        restored = client.patch(
            f"/api/v0/workspaces/{temporary_id}",
            headers=owner_headers,
            json={"status": "active", "name": "Restored"},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "active"
        assert restored.json()["name"] == "Restored"
        assert {
            item["id"]
            for item in client.get("/api/v0/workspaces", headers=owner_headers).json()
        } == {default_workspace_id, temporary_id}

        audit = client.get(
            "/api/v0/audit-events",
            headers={
                **owner_headers,
                "X-Workspace-ID": temporary_id,
            },
            params={"action": "workspace.updated", "target_id": temporary_id},
        )
        assert audit.status_code == 200
        assert len(audit.json()) == 2


def test_user_password_login_creates_expiring_revocable_session():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        user, api_key_headers = _onboard_password_user(client)
        initial_password = "initial-password-123"
        session_headers = _assert_password_enrollment_and_login(
            client, runtime, user, api_key_headers, initial_password
        )
        changed_password = "replacement-password-456"
        _assert_password_change_revokes_session(
            client,
            api_key_headers,
            session_headers,
            initial_password,
            changed_password,
        )
        _assert_logout_and_session_expiration(client, runtime, user, changed_password)


def _onboard_password_user(client):
    """Create a user and return its owner credential for password setup."""
    response = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "Session Org",
            "owner_email": "owner@sessions.test",
            "owner_display_name": "Owner",
        },
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    return response.json()["owner"], headers


def _assert_password_enrollment_and_login(
    client, runtime, user, api_key_headers, password
):
    """Enroll a password, verify hashing, and create an authenticated session."""
    enrollment = client.put(
        "/api/v0/auth/password",
        headers=api_key_headers,
        json={"password": password},
    )
    assert enrollment.status_code == 204
    stored = runtime.user_passwords.get(user["id"])
    assert password not in stored.password_hash
    assert stored.password_hash.startswith("pbkdf2_sha256$")
    invalid = client.post(
        "/api/v0/auth/login",
        json={"email": user["email"], "password": "incorrect-password"},
    )
    assert invalid.status_code == 401
    login = client.post(
        "/api/v0/auth/login", json={"email": user["email"], "password": password}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    current = client.get("/api/v0/auth/me", headers=headers)
    assert current.status_code == 200
    assert current.json()["user"]["id"] == user["id"]
    return headers


def _assert_password_change_revokes_session(
    client, api_key_headers, session_headers, current_password, replacement
):
    """Reject an incorrect current password and revoke sessions on change."""
    rejected = client.put(
        "/api/v0/auth/password",
        headers=api_key_headers,
        json={
            "current_password": "incorrect-current-password",
            "password": replacement,
        },
    )
    assert rejected.status_code == 422
    changed = client.put(
        "/api/v0/auth/password",
        headers=api_key_headers,
        json={"current_password": current_password, "password": replacement},
    )
    assert changed.status_code == 204
    assert client.get("/api/v0/auth/me", headers=session_headers).status_code == 401


def _assert_logout_and_session_expiration(client, runtime, user, password):
    """Verify logout revocation and rejection after a session expires."""
    login = client.post(
        "/api/v0/auth/login", json={"email": user["email"], "password": password}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/v0/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v0/auth/me", headers=headers).status_code == 401
    expiring = client.post(
        "/api/v0/auth/login", json={"email": user["email"], "password": password}
    )
    assert expiring.status_code == 200
    session = runtime.user_sessions.get(expiring.json()["session_id"])
    expired_at = datetime.now(UTC)
    runtime.user_sessions.put(
        session.model_copy(update={"expires_at": expired_at, "updated_at": expired_at})
    )
    headers = {"Authorization": f"Bearer {expiring.json()['access_token']}"}
    assert client.get("/api/v0/auth/me", headers=headers).status_code == 401


def test_login_api_limits_repeated_failures_without_revealing_account_state():
    runtime = LocalPlatformRuntime()
    runtime.login_rate_limiter.limit = 1
    client = TestClient(create_app(runtime))

    with client:
        onboarded = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Throttle Org",
                "owner_email": "owner@throttle.test",
                "owner_display_name": "Owner",
            },
        )
        assert onboarded.status_code == 201
        user = onboarded.json()["owner"]
        enrolled = client.put(
            "/api/v0/auth/password",
            headers={"Authorization": f"Bearer {onboarded.json()['access_token']}"},
            json={"password": "throttle-password-123"},
        )
        assert enrolled.status_code == 204

        first_failure = client.post(
            "/api/v0/auth/login",
            json={"email": user["email"], "password": "incorrect-password"},
        )
        throttled = client.post(
            "/api/v0/auth/login",
            json={"email": user["email"], "password": "throttle-password-123"},
        )

    assert first_failure.status_code == 401
    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "Too many sign-in attempts. Try again later."
    assert int(throttled.headers["retry-after"]) > 0


def test_member_provisioning_does_not_expose_a_signin_credential():
    with TestClient(create_app(LocalPlatformRuntime())) as client:
        roles = client.get("/api/v0/roles").json()
        role = next(r for r in roles if r["system_role_key"] == "user")
        created = client.post(
            "/api/v0/users",
            json={
                "email": "new.member@example.test",
                "display_name": "New member",
                "role_id": role["id"],
            },
        )
        assert created.status_code == 201, created.text
        assert "access_token" not in created.json()
        assert created.json()["user"]["status"] == "active"
        assert client.post("/api/v0/user-invitations").status_code == 404
