"""Onboarding cases shared by the memory and PostgreSQL test modules.

Each case takes a world built by ``onboarding_world`` (two organizations from
``organization_administration.prepare``: owners A and B and one shared Builder)
and drives the real HTTP routes with account bearer sessions.
"""

from __future__ import annotations

from typing import Any

from contracts.v1 import MembershipStatus
from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from shared.request_context import system_scope
from tests.support.account_session import sign_in
from tests.support.lifecycle_http import REQUEST
from tests.support.organization_administration import (
    TRANSFER,
    headers,
    prepare,
    scope,
    transfer_body,
)

ME = "/api/v0/account/me"
ORGS = "/api/v0/account/organizations"
SLUGS = "/api/v0/account/organization-slugs/"
SETTINGS = "/api/v0/organization/settings"
ENABLE = "WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION"
LIMIT = "WEAVES_ACCOUNT_ORGANIZATION_LIMIT"


def onboarding_world(runtime: Any, monkeypatch: Any) -> dict[str, Any]:
    """Enable creation with roomy budgets and seed the two organizations."""
    monkeypatch.setenv(ENABLE, "true")
    monkeypatch.setenv(LIMIT, "20")
    roomy(runtime)
    return {"runtime": runtime, **prepare(runtime)}


def roomy(runtime: Any) -> None:
    """Give creation and slug checks budgets no ordinary case can exhaust."""
    service = runtime.account_organization_service
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=500)
    service.slug_rate_limiter = InMemoryLoginRateLimiter(limit=500)


def bearer(token: str, **extra: str) -> dict[str, str]:
    """Headers for an account bearer call."""
    return {"Authorization": f"Bearer {token}", **extra}


def token_for(world: dict[str, Any], email: str) -> str:
    """Sign an email in (linking its memberships) and return a bearer token."""
    with system_scope("account"):
        _, token = sign_in(world["runtime"].account_sign_in_service, email)
    return token


def me(world: dict[str, Any], token: str) -> dict[str, Any]:
    """The account's own ``/account/me`` payload."""
    response = world["client"].get(ME, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def membership(world: dict[str, Any], token: str, org_id: str) -> dict[str, Any]:
    """This account's membership row for one organization."""
    return next(
        m for m in me(world, token)["memberships"] if m["organization_id"] == org_id
    )


def create(world: dict[str, Any], token: str, key: str, **body: Any) -> Any:
    """POST an organization creation with an idempotency key."""
    return world["client"].post(
        ORGS, json=body, headers=bearer(token, **{"Idempotency-Key": key})
    )


def patch_me(world: dict[str, Any], token: str, body: Any) -> Any:
    """PATCH the account profile."""
    return world["client"].patch(ME, json=body, headers=bearer(token))


INVALID_PROFILES: tuple[Any, ...] = (
    {"role_title": ""},
    {"role_title": "   "},
    {"role_title": "x" * 81},
    {"role_title": "line\nbreak"},
    {"role_title": "bell\x07"},
    {"role_title": 5},
    {"display_name": None},
    {"display_name": None, "role_title": "Ops"},
    {},
    {"role_title": "Ops", "status": "disabled"},
)


def role_title_case(world: dict[str, Any]) -> None:
    """Set, combine, validate and clear the role or team; nothing leaves the account."""
    token, other = world["owner_token"], world["b_token"]
    name = me(world, token)["account"]["display_name"]
    assert me(world, token)["account"]["role_title"] is None
    done = patch_me(world, token, {"role_title": "  Finance Ops  "})
    assert done.status_code == 200, done.text
    assert done.json()["account"]["role_title"] == "Finance Ops"
    assert done.json()["account"]["display_name"] == name
    assert me(world, token)["account"]["role_title"] == "Finance Ops"
    assert me(world, other)["account"]["role_title"] is None
    for body in INVALID_PROFILES:
        assert patch_me(world, token, body).status_code == 422, body
    assert me(world, token)["account"] == done.json()["account"]
    both = patch_me(world, token, {"display_name": "Owner Ay", "role_title": "r" * 80})
    assert both.status_code == 200
    assert both.json()["account"]["role_title"] == "r" * 80
    renamed = patch_me(world, token, {"display_name": "Owner A2"})
    assert renamed.json()["account"]["role_title"] == "r" * 80
    cleared = patch_me(world, token, {"role_title": None})
    assert cleared.status_code == 200
    assert cleared.json()["account"]["role_title"] is None
    assert cleared.json()["account"]["display_name"] == "Owner A2"
    assert patch_me(world, other, {"role_title": "Legal"}).status_code == 200
    assert me(world, token)["account"]["role_title"] is None
    with scope(world["b"]):
        users = world["runtime"].users.list_scoped(world["b"][0].id)
    assert all("Legal" not in user.model_dump_json() for user in users)


def _standing(row: dict[str, Any]) -> tuple[bool, str]:
    return row["is_owner"], row["role_label"]


def membership_roles_case(world: dict[str, Any]) -> None:
    """Owner, Builder, Admin after transfer, suspended and pending deletion."""
    a, b = world["a"][0].id, world["b"][0].id
    owner, member, other = (
        world["owner_token"],
        world["member_token"],
        world["b_token"],
    )
    assert _standing(membership(world, owner, a)) == (True, "Owner")
    assert membership(world, owner, a)["organization_slug"] == "org-a"
    assert _standing(membership(world, member, a)) == (False, "Builder")
    assert _standing(membership(world, member, b)) == (False, "Builder")
    foreign = me(world, other)["memberships"]
    assert [m["organization_id"] for m in foreign] == [b]
    assert _standing(foreign[0]) == (True, "Owner")
    assert [m["organization_id"] for m in me(world, owner)["memberships"]] == [a]

    moved = world["client"].post(
        TRANSFER, headers=headers(world), json=transfer_body(world)
    )
    assert moved.status_code == 200, moved.text
    assert _standing(membership(world, member, a)) == (True, "Owner")
    assert _standing(membership(world, owner, a)) == (False, "Admin")
    assert _standing(membership(world, member, b)) == (False, "Builder")

    runtime = world["runtime"]
    with scope(world["a"]):
        link = next(
            m
            for m in runtime.memberships.list_scoped(a)
            if m.principal_id == world["a"][3].id
        )
        runtime.memberships.put(
            link.model_copy(update={"status": MembershipStatus.SUSPENDED})
        )
    suspended = membership(world, owner, a)
    assert suspended["membership_status"] == "suspended"
    assert _standing(suspended) == (False, "Admin")

    asked = world["client"].post(
        REQUEST, headers=headers(world, "member_token"), json={"confirm_name": "Org A"}
    )
    assert asked.status_code == 200, asked.text
    pending = membership(world, member, a)
    assert pending["lifecycle_state"] == "deletion_requested"
    assert _standing(pending) == (True, "Owner")


def creation_settings_case(world: dict[str, Any]) -> None:
    """Timezone and locale are stored with the organization and returned."""
    token = token_for(world, "founder@example.test")
    made = create(
        world, token, "tz-1", name="Acme", timezone="America/New_York", locale="en-US"
    )
    assert made.status_code == 201, made.text
    organization = made.json()["organization"]
    assert organization["timezone"] == "America/New_York"
    assert organization["locale"] == "en-US"
    assert organization["slug"] == "acme"
    selected = bearer(token, **{"X-Organization-Id": organization["id"]})
    stored = world["client"].get(SETTINGS, headers=selected).json()["settings"]
    assert (stored["timezone"], stored["locale"]) == ("America/New_York", "en-US")
    assert stored["slug"] == "acme" and stored["revision"] == 1
    replay = create(
        world, token, "tz-1", name="Acme", timezone="America/New_York", locale="en-US"
    )
    assert replay.status_code == 200 and replay.json() == made.json()
    row = membership(world, token, organization["id"])
    assert _standing(row) == (True, "Owner") and row["organization_slug"] == "acme"

    plain = create(world, token, "tz-2", name="Beta Co").json()["organization"]
    assert (plain["timezone"], plain["locale"]) == ("Asia/Kolkata", "en")
    bad_zone = create(world, token, "tz-3", name="Gamma", timezone="Mars/Phobos")
    assert bad_zone.status_code == 422
    assert bad_zone.json()["detail"] == "timezone must be an IANA time zone name"
    bad_locale = create(world, token, "tz-3", name="Gamma", locale="not a locale")
    assert bad_locale.status_code == 422 and "locale" in bad_locale.json()["detail"]
    assert bad_zone.headers["x-request-id"] != bad_locale.headers["x-request-id"]
    assert len(me(world, token)["memberships"]) == 2


def throttle_case(world: dict[str, Any]) -> None:
    """Creation attempts beyond the budget get 429 with Retry-After seconds."""
    service = world["runtime"].account_organization_service
    token = token_for(world, "eager@example.test")
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=1)
    try:
        assert create(world, token, "t-1", name="First").status_code == 201
        limited = create(world, token, "t-2", name="Second")
    finally:
        roomy(world["runtime"])
    assert limited.status_code == 429
    seconds = int(limited.headers["retry-after"])
    assert 1 <= seconds <= 900
    assert limited.json()["detail"] == (
        f"Too many requests. Try again in {seconds} seconds."
    )
    assert limited.headers["x-request-id"]
    assert len(me(world, token)["memberships"]) == 1


def cap_case(world: dict[str, Any], monkeypatch: Any) -> None:
    """The per-account cap keeps its 403 and shows as a reason on ``me``."""
    token = token_for(world, "capped@example.test")
    monkeypatch.setenv(LIMIT, "1")
    assert me(world, token)["organization_creation"]["allowed"] is True
    assert create(world, token, "c-1", name="Only").status_code == 201
    capped = create(world, token, "c-2", name="More")
    assert capped.status_code == 403
    assert capped.json()["detail"] == "This account has reached its organization limit"
    status = me(world, token)["organization_creation"]
    assert (status["allowed"], status["reason"]) == (False, "limit")
    monkeypatch.setenv(ENABLE, "false")
    monkeypatch.setenv(LIMIT, "20")
    status = me(world, token)["organization_creation"]
    assert (status["allowed"], status["reason"]) == (False, "disabled")
    assert status["domain"] is None and status["domain_organization_name"] is None
