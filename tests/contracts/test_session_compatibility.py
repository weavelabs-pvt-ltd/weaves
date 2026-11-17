"""Session wire compatibility across the pre-browser and SSO releases."""

import json

from pydantic import AwareDatetime

from contracts.v1 import UserSession, UserSessionStatus
from contracts.v1.base import MutableOrgScopedContract, OpaqueId
from tests.support.runtime import LocalPlatformRuntime


class LegacyUserSession(MutableOrgScopedContract):
    """Frozen pre-browser session shape, including its rejection of extra fields."""

    id: OpaqueId
    principal_id: OpaqueId
    token_digest: str
    status: UserSessionStatus
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None


def test_bearer_sessions_remain_readable_by_the_previous_release():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    principal = runtime.principals.get("local-developer")
    session, token = runtime.authentication_service.create_user_session(principal)
    payload = session.model_dump_json()
    legacy = LegacyUserSession.model_validate_json(payload)
    assert legacy.id == session.id
    assert "channel" not in json.loads(payload)
    assert (
        runtime.authentication_service.authenticate_user_session(token) == principal.id
    )
    explicit = dict(json.loads(payload), channel="bearer")
    assert UserSession.model_validate_json(json.dumps(explicit)).channel == "bearer"


def test_browser_records_preserve_their_channel_and_cannot_be_used_as_bearers():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    principal = runtime.principals.get("local-developer")
    session, token = runtime.authentication_service.create_user_session(principal)
    browser = session.model_copy(update={"channel": "browser"})
    assert json.loads(browser.model_dump_json())["channel"] == "browser"
    runtime.user_sessions.put(browser)
    stored = runtime.user_sessions.get(session.id)
    assert stored.channel == "browser"
    assert runtime.authentication_service.authenticate_user_session(token) is None
