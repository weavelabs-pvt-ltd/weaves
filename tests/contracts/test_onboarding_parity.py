"""Onboarding design parity on memory repositories: profile, roles, slugs, domains."""

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import Role
from modules.auth.account_membership_roles import role_label
from modules.auth.organization_email_domains import custom_domain, custom_domains
from modules.auth.organization_slugs import SlugRejected, normalize_slug, slugify
from shared.organization_naming import PUBLIC_EMAIL_DOMAINS, RESERVED_SLUGS
from tests.support import onboarding_cases as cases
from tests.support import organization_identity_cases as identity
from tests.support.account_session import google_headers
from tests.support.google_world import EMAIL, google_world

__all__ = ["google_world"]

BROWSER_ME = "/api/v0/account/browser/me"


@pytest.fixture
def world(monkeypatch):
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    world = cases.onboarding_world(runtime, monkeypatch)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        world["client"] = client
        yield world
    runtime.close()


def test_role_title_is_set_validated_cleared_and_private_to_the_account(world):
    cases.role_title_case(world)


def test_memberships_say_who_owns_and_which_role_to_show(world):
    cases.membership_roles_case(world)


def test_creation_stores_and_returns_timezone_and_locale(world):
    cases.creation_settings_case(world)


def test_throttled_creation_answers_429_with_retry_after(world):
    cases.throttle_case(world)


def test_the_cap_and_the_disabled_flag_show_on_the_me_payload(world, monkeypatch):
    cases.cap_case(world, monkeypatch)


def test_slug_availability_is_exact_private_and_budgeted(world):
    identity.slug_availability_case(world)


def test_creation_reserves_an_explicit_or_derived_slug(world):
    identity.slug_creation_case(world)


def test_an_admin_changes_the_slug_and_export_includes_it(world):
    identity.slug_settings_case(world)


def test_a_custom_email_domain_belongs_to_one_organization(world):
    identity.domain_claim_case(world)


def test_the_domain_kill_switch_and_the_extended_public_list(world, monkeypatch):
    identity.domain_switches_case(world, monkeypatch)


def test_racing_creators_on_one_custom_domain_yield_one_organization(world):
    identity.domain_race_case(world)


def test_racing_creators_on_one_slug_yield_one_holder(world):
    identity.slug_race_case(world)


def _role(name, key=None):
    return Role(id=name, org_id="o", name=name, permissions=[], system_role_key=key)


def test_the_role_label_rule_is_deterministic():
    admin, builder, user = (
        _role("Admin", "admin"),
        _role("Builder", "builder"),
        _role("User", "user"),
    )
    custom = [_role("zeta"), _role("Auditor"), _role("auditor")]
    assert role_label([user, builder, admin, *custom]) == "Admin"
    assert role_label([user, builder, *custom]) == "Builder"
    assert role_label([*custom, user]) == "User"
    assert role_label(custom) == "Auditor"
    assert role_label(list(reversed(custom))) == "Auditor"
    assert role_label([]) == ""


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("acme", "acme"),
        (" Acme-Inc ", "acme-inc"),
        ("a1b", "a1b"),
        ("x" * 40, "x" * 40),
    ],
)
def test_valid_slugs_are_normalized(raw, slug):
    assert normalize_slug(raw) == slug


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ab",
        "x" * 41,
        "-acme",
        "acme-",
        "ac--me",
        "ac_me",
        "ac me",
        "ac.me",
        "acmé",
        "acme/team",
    ],
)
def test_malformed_slugs_are_invalid(raw):
    with pytest.raises(SlugRejected) as refused:
        normalize_slug(raw)
    assert refused.value.reason == "invalid"


def test_every_reserved_word_is_refused_in_any_case():
    assert {"admin", "api", "app", "www", "weaves", "new"} <= RESERVED_SLUGS
    assert len(RESERVED_SLUGS) == 15
    for word in RESERVED_SLUGS:
        with pytest.raises(SlugRejected) as refused:
            normalize_slug(word.upper())
        assert refused.value.reason == "reserved"


def test_slugify_always_yields_a_well_formed_slug():
    for text in ("Acme Inc.", "  --A--  ", "", "é", "x" * 99, "a-" * 30, "Ünï cødé"):
        slug = slugify(text)
        assert 3 <= len(slug) <= 40, text
        assert slug == slug.strip("-") and "--" not in slug, text
    assert slugify("Acme Inc.") == "acme-inc"
    assert slugify("") == "org"


def test_only_custom_domains_can_be_claimed(monkeypatch):
    assert {"gmail.com", "outlook.com", "yahoo.co.in", "naver.com"} <= (
        PUBLIC_EMAIL_DOMAINS
    )
    assert custom_domain("Pat@Gmail.com") is None
    assert custom_domain("Pat@Corp.Example") == "corp.example"
    assert custom_domain("not-an-email") is None
    assert custom_domains(["a@b.test", "c@B.test", "d@gmail.com"]) == ["b.test"]
    monkeypatch.setenv("WEAVES_PUBLIC_EMAIL_DOMAINS", "B.test")
    assert custom_domain("a@b.test") is None


def test_the_cookie_routes_carry_the_same_profile_and_membership_fields(
    google_world, monkeypatch
):
    monkeypatch.setenv(cases.ENABLE, "true")
    world = google_world()
    client = world.client
    headers = google_headers(client, world.fake, EMAIL)
    body = {"role_title": " Finance Ops "}
    assert client.patch(BROWSER_ME, json=body).status_code == 403
    without_csrf = {"Origin": headers["Origin"]}
    assert client.patch(BROWSER_ME, json=body, headers=without_csrf).status_code == 403
    done = client.patch(BROWSER_ME, json=body, headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["account"]["role_title"] == "Finance Ops"
    for invalid in cases.INVALID_PROFILES:
        refused = client.patch(BROWSER_ME, json=invalid, headers=headers)
        assert refused.status_code == 422, invalid
    payload = client.get(BROWSER_ME).json()
    assert payload["account"]["role_title"] == "Finance Ops"
    (row,) = payload["memberships"]
    assert (row["is_owner"], row["role_label"]) == (True, "Owner")
    assert row["organization_slug"] == "org-a"
    assert payload["organization_creation"] == identity.OPEN
    cleared = client.patch(BROWSER_ME, json={"role_title": None}, headers=headers)
    assert cleared.json()["account"]["role_title"] is None
    slug = client.get("/api/v0/account/browser/organization-slugs/org-a")
    assert slug.status_code == 200 and slug.json()["reason"] == "taken"
    assert slug.headers["x-request-id"]
    client.cookies.clear()
    gone = client.get("/api/v0/account/browser/organization-slugs/org-a")
    assert gone.status_code == 401 and gone.headers["x-request-id"]


def test_every_response_has_a_request_id_and_cors_exposes_it(world):
    client = world["client"]
    owner = cases.bearer(world["owner_token"])
    seen = set()
    for response in (
        client.get("/api/v0/health"),
        client.get("/api/v0/account/me"),
        client.get("/api/v0/agents"),
        client.patch("/api/v0/account/me", json={}, headers=owner),
        client.get("/api/v0/account/no-such-route", headers=owner),
    ):
        seen.add(response.headers["x-request-id"])
    assert len(seen) == 5 and all(len(value) >= 8 for value in seen)
    cross = client.get("/api/v0/health", headers={"Origin": "http://localhost:5173"})
    assert "X-Request-Id" in cross.headers["access-control-expose-headers"]
    assert "Retry-After" in cross.headers["access-control-expose-headers"]
