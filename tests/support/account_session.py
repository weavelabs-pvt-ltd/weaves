"""Sign a person in through the real account flow and return request headers."""

from __future__ import annotations

import re
from typing import Any

HMAC_KEY = "test-account-session-key-" * 2


class _CapturedMail:
    """Capture only the email transport boundary for one sign-in."""

    configured = True
    code = ""

    def send(self, recipient: str, subject: str, body: str) -> None:
        """Record the generated code without weakening the identity service."""
        match = re.search(r"\b\d{6}\b", body)
        assert match is not None
        self.code = match.group()


def sign_in(service: Any, email: str) -> tuple[Any, str]:
    """Run email-code sign-in on an account service; return (account, token).

    The sender and HMAC key are swapped only for the duration of the call, so the
    service itself is exercised exactly as in production (codes, throttles,
    membership linking).
    """
    previous = service.email_sender, service.email_login_hmac_key
    mail = _CapturedMail()
    try:
        service.email_sender, service.email_login_hmac_key = mail, HMAC_KEY
        challenge = service.start_email_login(email)
        account, _, token = service.verify_email_login(challenge, mail.code)
    finally:
        service.email_sender, service.email_login_hmac_key = previous
    return account, token


def account_headers(
    client: Any,
    email: str,
    organization_id: str,
    workspace_id: str | None = None,
) -> dict[str, str]:
    """Sign ``email`` in, link its memberships and return tenant request headers.

    ``client`` is a started TestClient. The returned headers carry the account
    bearer session and ``X-Organization-Id`` (plus ``X-Workspace-ID`` if given).
    """
    service = client.app.state.authentication_services.accounts
    _, token = sign_in(service, email)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": organization_id,
    }
    if workspace_id is not None:
        headers["X-Workspace-ID"] = workspace_id
    return headers
