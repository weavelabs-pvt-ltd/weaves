"""Provisioned member access and actual one-time-code session lifecycle."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import UserStatus
from shared.clock import utc_now
from tests.support.runtime import LocalPlatformRuntime


class CapturedMail:
    """Capture the transport boundary without bypassing authentication."""

    configured = True

    def __init__(self):
        """Retain messages only inside this isolated test runtime."""
        self.messages = []

    def send(self, recipient, subject, body):
        """Record the requested message as an SMTP adapter would receive it."""
        self.messages.append((recipient, subject, body))

    def code(self):
        """Extract the real random code that the authentication service issued."""
        return re.search(r"\b\d{6}\b", self.messages[-1][2]).group()


@pytest.fixture
def member_runtime():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    mail = CapturedMail()
    auth = runtime.authentication_service
    auth.email_sender = mail
    auth.email_login_hmac_key = "isolated-test-key-" * 3
    role = next(r for r in runtime.roles.list() if r.system_role_key == "user")
    user, principal, _ = runtime.user_administration_service.create_workspace_user(
        email="newmember@example.com", display_name="New Member", role_id=role.id
    )
    yield runtime, auth, mail, user, principal
    runtime.close()


def test_provision_code_verify_and_account_status_are_separate(member_runtime):
    runtime, _auth, mail, user, principal = member_runtime
    assert user.status is UserStatus.ACTIVE
    with TestClient(create_app(runtime)) as client:
        rows = client.get("/api/v0/users").json()
        assert next(u for u in rows if u["id"] == user.id)["account_status"] == "never"
        response = client.post("/api/v0/auth/email-code", json={"email": user.email})
        assert response.status_code == 202
        challenge = response.json()["challenge_id"]
        assert "code" not in response.json()
        verified = client.post(
            "/api/v0/auth/email-verify",
            json={"challenge_id": challenge, "code": mail.code()},
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["principal"]["id"] == principal.id
        assert verified.json()["access_token"]
        rows = client.get("/api/v0/users").json()
        row = next(u for u in rows if u["id"] == user.id)
        assert row["account_status"] == "signed_in"
        assert row["last_signed_in_at"]
        assert "token_digest" not in str(rows)
        replay = client.post(
            "/api/v0/auth/email-verify",
            json={"challenge_id": challenge, "code": mail.code()},
        )
        assert replay.status_code == 401


def test_resend_revokes_previous_challenge_and_expiry_is_enforced(member_runtime):
    runtime, auth, mail, user, _ = member_runtime
    first = auth.start_email_login(user.email)
    old_code = mail.code()
    second = auth.start_email_login(user.email)
    with pytest.raises(PermissionError):
        auth.verify_email_login(first, old_code)
    challenge = runtime.email_login_challenges.get(second)
    runtime.email_login_challenges.put(
        challenge.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    with pytest.raises(PermissionError):
        auth.verify_email_login(second, mail.code())


def test_unknown_or_paused_accounts_cannot_start_a_session(member_runtime):
    runtime, auth, mail, user, _ = member_runtime
    unknown = auth.start_email_login("missing@example.com")
    assert not mail.messages
    with pytest.raises(PermissionError):
        auth.verify_email_login(unknown, "000000")
    challenge = auth.start_email_login(user.email)
    code = mail.code()
    runtime.user_administration_service.suspend_workspace_user(user.id)
    with pytest.raises(PermissionError):
        auth.verify_email_login(challenge, code)
