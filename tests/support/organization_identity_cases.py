"""Slug and email-domain cases shared by the memory and PostgreSQL test modules."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from modules.auth.account_organization_input import AccountOrganizationCommand
from modules.auth.organization_email_domains import DomainBelongsToOrganization
from modules.auth.organization_slugs import SlugTaken
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.account_session import sign_in
from tests.support.lifecycle_http import EXPORT
from tests.support.onboarding_cases import (
    SETTINGS,
    SLUGS,
    bearer,
    create,
    me,
    membership,
    roomy,
    token_for,
)
from tests.support.organization_administration import headers

ANSWER_KEYS = {"slug", "valid", "available", "suggestion", "reason"}
DOMAIN = "domain_belongs_to_organization"
OPEN = {
    "allowed": True,
    "reason": None,
    "domain": None,
    "domain_organization_name": None,
    "domain_membership": None,
}


def check(world: dict[str, Any], token: str, slug: str) -> dict[str, Any]:
    """Ask the availability route about one slug."""
    response = world["client"].get(SLUGS + slug, headers=bearer(token))
    assert response.status_code == 200, response.text
    assert set(response.json()) == ANSWER_KEYS
    assert response.headers["cache-control"] == "no-store"
    assert "Org A" not in response.text and world["a"][0].id not in response.text
    return response.json()


def slug_availability_case(world: dict[str, Any]) -> None:
    """Availability answers are exact, name nobody, need a session and are budgeted."""
    token = world["b_token"]
    assert check(world, token, "org-a") == {
        "slug": "org-a",
        "valid": True,
        "available": False,
        "suggestion": "org-a-team",
        "reason": "taken",
    }
    assert check(world, token, "ORG-A")["reason"] == "taken"
    free = check(world, token, " Fresh-Name ")
    assert free == {
        "slug": "fresh-name",
        "valid": True,
        "available": True,
        "suggestion": None,
        "reason": None,
    }
    reserved = check(world, token, "Admin")
    assert (reserved["valid"], reserved["available"]) == (False, False)
    assert (reserved["reason"], reserved["suggestion"]) == ("reserved", "admin-team")
    for bad in ("ab", "-lead", "trail-", "two--hyphens", "under_score", "x" * 41):
        answer = check(world, token, bad)
        assert (answer["valid"], answer["reason"]) == (False, "invalid"), bad
    assert check(world, token, "Has Space")["suggestion"] == "has-space"
    assert check(world, token, "Org A!")["suggestion"] == "org-a-team"

    client = world["client"]
    assert client.get(SLUGS + "org-a").status_code == 401
    browser = "/api/v0/account/browser/organization-slugs/org-a"
    assert client.get(browser).status_code == 401
    assert client.get(browser, headers=bearer(token)).status_code == 401

    service = world["runtime"].account_organization_service
    service.slug_rate_limiter = InMemoryLoginRateLimiter(limit=2)
    try:
        codes = [client.get(SLUGS + "org-a", headers=bearer(token)) for _ in range(3)]
    finally:
        roomy(world["runtime"])
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert int(codes[2].headers["retry-after"]) >= 1
    assert "organization" not in codes[2].json()["detail"].lower()


def _refusal(response: Any, status: int, reason: str, suggestion: str | None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"detail", "code", "reason", "slug", "suggestion"}
    assert (body["reason"], body["code"]) == (reason, f"slug_{reason}")
    assert body["suggestion"] == suggestion
    assert isinstance(body["detail"], str) and "Org A" not in response.text


def slug_creation_case(world: dict[str, Any]) -> None:
    """Explicit slugs are exact or refused; derived slugs find the first free one."""
    token = token_for(world, "slugger@example.test")
    before = len(me(world, token)["memberships"])
    explicit = create(world, token, "s-1", name="Any Name", slug=" My-Team ")
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["organization"]["slug"] == "my-team"
    _refusal(
        create(world, token, "s-2", name="Other", slug="Org-A"),
        409,
        "taken",
        "org-a-team",
    )
    _refusal(
        create(world, token, "s-3", name="Other", slug="admin"),
        422,
        "reserved",
        "admin-team",
    )
    _refusal(
        create(world, token, "s-4", name="Other", slug="no"), 422, "invalid", "no-org"
    )
    _refusal(
        create(world, token, "s-5", name="Other", slug="Bad Slug!"),
        422,
        "invalid",
        "bad-slug",
    )
    assert len(me(world, token)["memberships"]) == before + 1

    def derived(key: str, name: str) -> str:
        made = create(world, token, key, name=name)
        assert made.status_code == 201, made.text
        return made.json()["organization"]["slug"]

    assert derived("d-1", "Org A") == "org-a-team"
    assert derived("d-2", "  org  a ") == "org-a-2"
    assert derived("d-3", "ORG.A") == "org-a-3"
    assert derived("d-4", "Admin") == "admin-team"
    assert derived("d-5", "!!!") == "org"
    assert derived("d-6", "X" * 60) == "x" * 40
    assert derived("d-7", "x" * 60) == "x" * 35 + "-team"
    replay = create(world, token, "d-1", name="Org A")
    assert replay.status_code == 200
    assert replay.json()["organization"]["slug"] == "org-a-team"


def _settings_body(world: dict[str, Any], **changes: Any) -> dict[str, Any]:
    current = world["client"].get(SETTINGS, headers=headers(world)).json()
    return {
        "name": current["organization"]["name"],
        "timezone": current["settings"]["timezone"],
        "locale": current["settings"]["locale"],
        "expected_revision": current["settings"]["revision"],
        **changes,
    }


def slug_settings_case(world: dict[str, Any]) -> None:
    """An admin changes the slug under the revision check; members cannot."""
    client, owner = world["client"], headers(world)
    initial = client.get(SETTINGS, headers=owner).json()["settings"]
    assert initial["slug"] == "org-a" and initial["email_domain"] is None
    kept = client.patch(SETTINGS, headers=owner, json=_settings_body(world))
    assert kept.status_code == 200 and kept.json()["settings"]["revision"] == 1
    member = client.patch(
        SETTINGS,
        headers=headers(world, "member_token"),
        json=_settings_body(world, slug="member-pick"),
    )
    assert member.status_code == 403
    _refusal(
        client.patch(SETTINGS, headers=owner, json=_settings_body(world, slug="Org-B")),
        409,
        "taken",
        "org-b-team",
    )
    _refusal(
        client.patch(SETTINGS, headers=owner, json=_settings_body(world, slug="api")),
        422,
        "reserved",
        "api-team",
    )
    _refusal(
        client.patch(SETTINGS, headers=owner, json=_settings_body(world, slug="a b")),
        422,
        "invalid",
        "a-b",
    )
    assert client.get(SETTINGS, headers=owner).json()["settings"] == initial

    changed = client.patch(
        SETTINGS, headers=owner, json=_settings_body(world, slug="Renamed-A")
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["settings"]["slug"] == "renamed-a"
    assert changed.json()["settings"]["revision"] == 2
    stale = client.patch(
        SETTINGS,
        headers=owner,
        json=_settings_body(world, slug="again-a", expected_revision=1),
    )
    assert stale.status_code == 409 and isinstance(stale.json()["detail"], str)
    row = membership(world, world["owner_token"], world["a"][0].id)
    assert row["organization_slug"] == "renamed-a"
    assert check(world, world["b_token"], "org-a")["available"] is True
    assert check(world, world["b_token"], "renamed-a")["reason"] == "taken"
    same = client.patch(
        SETTINGS, headers=owner, json=_settings_body(world, slug="renamed-a")
    )
    assert same.status_code == 200 and same.json()["settings"]["revision"] == 2
    document = client.post(EXPORT, headers=owner).json()
    assert document["settings"]["slug"] == "renamed-a"
    assert "email_domain" in document["settings"]


def _add_member(world: dict[str, Any], created: dict[str, Any], email: str) -> None:
    """Add a person to an account-created organization, as People does."""
    runtime = world["runtime"]
    org_id = created["organization"]["id"]
    identity = set_request_identity(
        created["membership"]["principal_id"], org_id, created["workspace"]["id"]
    )
    try:
        role = next(
            r for r in runtime.roles.list_scoped(org_id) if r.system_role_key == "user"
        )
        runtime.user_administration_service.create_workspace_user(
            email=email, display_name="Added Person", role_id=role.id
        )
    finally:
        reset_request_principal(identity)


def domain_claim_case(world: dict[str, Any]) -> None:
    """The first organization claims a custom domain; nobody on it creates another."""
    ana = token_for(world, "ana@corp.test")
    assert me(world, ana)["organization_creation"] == OPEN
    made = create(world, ana, "corp-1", name="Corp")
    assert made.status_code == 201, made.text
    assert made.json()["organization"]["email_domain"] == "corp.test"
    selected = bearer(ana, **{"X-Organization-Id": made.json()["organization"]["id"]})
    settings = world["client"].get(SETTINGS, headers=selected).json()["settings"]
    assert settings["email_domain"] == "corp.test"
    claimed = {
        "allowed": False,
        "reason": DOMAIN,
        "domain": "corp.test",
        "domain_organization_name": "Corp",
        "domain_membership": "member",
    }
    assert me(world, ana)["organization_creation"] == claimed
    assert create(world, ana, "corp-1", name="Corp").status_code == 200

    bo = token_for(world, "Bo@CORP.test")
    outsider = {**claimed, "domain_membership": "not_member"}
    assert me(world, bo)["organization_creation"] == outsider
    assert me(world, bo)["memberships"] == []
    for token in (ana, bo):
        refused = create(world, token, "corp-2", name="Second Corp")
        assert refused.status_code == 409, refused.text
        assert set(refused.json()) == {"detail", "code", "organization_name"}
        assert refused.json()["code"] == DOMAIN
        assert refused.json()["organization_name"] == "Corp"
    _add_member(world, made.json(), "bo@corp.test")
    bo = token_for(world, "bo@corp.test")
    assert me(world, bo)["organization_creation"] == claimed
    assert membership(world, bo, made.json()["organization"]["id"])["role_label"] == (
        "User"
    )
    assert create(world, bo, "corp-3", name="Third Corp").status_code == 409

    pat = token_for(world, "pat@example.test")
    assert me(world, pat)["organization_creation"] == OPEN
    for key, name in (("p-1", "Pat One"), ("p-2", "Pat Two")):
        public = create(world, pat, key, name=name)
        assert public.status_code == 201
        assert public.json()["organization"]["email_domain"] is None
    elsewhere = token_for(world, "eve@elsewhere.test")
    for token in (pat, elsewhere, world["b_token"]):
        assert (
            "Corp"
            not in world["client"].get("/api/v0/account/me", headers=bearer(token)).text
        )
        assert me(world, token)["organization_creation"] == OPEN


def domain_switches_case(world: dict[str, Any], monkeypatch: Any) -> None:
    """The kill switch restores the old rule; the public list can be extended."""
    first = token_for(world, "one@switch.test")
    second = token_for(world, "two@switch.test")
    assert create(world, first, "sw-1", name="Switch One").status_code == 201
    assert create(world, second, "sw-2", name="Switch Two").status_code == 409
    monkeypatch.setenv("WEAVES_ENFORCE_ORGANIZATION_DOMAINS", "false")
    assert me(world, second)["organization_creation"] == OPEN
    allowed = create(world, second, "sw-2", name="Switch Two")
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["organization"]["email_domain"] is None
    assert create(world, first, "sw-3", name="Switch Three").status_code == 201
    monkeypatch.setenv("WEAVES_ENFORCE_ORGANIZATION_DOMAINS", "true")
    assert me(world, second)["organization_creation"]["reason"] == DOMAIN
    assert (
        me(world, second)["organization_creation"]["domain_organization_name"]
        == "Switch One"
    )

    monkeypatch.setenv("WEAVES_PUBLIC_EMAIL_DOMAINS", "example.test, FreeMail.test ")
    free = token_for(world, "kim@freemail.test")
    for key in ("f-1", "f-2"):
        made = create(world, free, key, name=f"Free {key}")
        assert made.status_code == 201
        assert made.json()["organization"]["email_domain"] is None
    assert me(world, free)["organization_creation"] == OPEN


def _race(operations: list[Any]) -> list[Any]:
    """Start the operations together and return results or the exception raised."""
    barrier = Barrier(len(operations))

    def run(operation: Any) -> Any:
        barrier.wait(timeout=10)
        try:
            with system_scope("account"):
                return operation()
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def _accounts(world: dict[str, Any], emails: list[str]) -> list[Any]:
    with system_scope("account"):
        service = world["runtime"].account_sign_in_service
        return [sign_in(service, email)[0] for email in emails]


def domain_race_case(world: dict[str, Any]) -> None:
    """Creators racing on one fresh custom domain: exactly one organization wins."""
    service = world["runtime"].account_organization_service
    emails = [f"racer{n}@race.test" for n in range(4)]
    accounts = _accounts(world, emails)
    outcomes = _race(
        [
            lambda account=account, n=n: service.create(
                account,
                AccountOrganizationCommand.build(name=f"Race {n}"),
                f"race-{n}",
                None,
            )
            for n, account in enumerate(accounts)
        ]
    )
    winners = [o for o in outcomes if not isinstance(o, Exception)]
    losers = [o for o in outcomes if isinstance(o, Exception)]
    assert len(winners) == 1, outcomes
    assert winners[0].settings.email_domain == "race.test"
    assert all(isinstance(o, DomainBelongsToOrganization) for o in losers), losers
    assert {o.claim.organization_name for o in losers} == {winners[0].organization.name}
    assert service.domains.claim_of("RACE.test").organization_id == (
        winners[0].organization.id
    )


def slug_race_case(world: dict[str, Any]) -> None:
    """Racing for one slug: one wins, explicit losers are refused, derived move on."""
    service = world["runtime"].account_organization_service
    accounts = _accounts(world, [f"slug{n}@example.test" for n in range(4)])

    def attempt(account: Any, n: int, **body: Any) -> Any:
        return lambda: service.create(
            account, AccountOrganizationCommand.build(**body), f"slug-race-{n}", None
        )

    explicit = _race(
        [
            attempt(account, n, name=f"Wanted {n}", slug="Wanted")
            for n, account in enumerate(accounts)
        ]
    )
    winners = [o for o in explicit if not isinstance(o, Exception)]
    losers = [o for o in explicit if isinstance(o, Exception)]
    assert len(winners) == 1 and winners[0].settings.slug == "wanted", explicit
    assert all(isinstance(o, SlugTaken) for o in losers), losers
    assert {o.suggestion for o in losers} == {"wanted-team"}

    derived = _race(
        [
            attempt(account, n + 10, name="Same Name")
            for n, account in enumerate(accounts)
        ]
    )
    assert not [o for o in derived if isinstance(o, Exception)], derived
    assert sorted(o.settings.slug for o in derived) == [
        "same-name",
        "same-name-2",
        "same-name-3",
        "same-name-team",
    ]
