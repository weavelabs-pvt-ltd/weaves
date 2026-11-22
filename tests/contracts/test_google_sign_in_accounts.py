"""Google sign-in account resolution, allowlists, memberships and the owner."""

from __future__ import annotations

import pytest

from contracts.v1 import (
    AccountIdentityKind,
    AccountStatus,
    MembershipStatus,
    PrincipalStatus,
    UserStatus,
)
from tests.support.account_session import google_headers, google_sign_in
from tests.support.account_world import seed_member, seed_organization
from tests.support.fake_google import ISSUER
from tests.support.google_world import EMAIL, google_world
from tests.support.operator_scripts import bootstrap_account_owner, run_script

__all__ = ["google_world"]

ERROR = "/#account-signin?error="


def _identities(world):
    return sorted(
        (i.kind.value, i.issuer, i.subject)
        for i in world.runtime.account_identities.list()
    )


def _counts(world):
    runtime = world.runtime
    return (
        len(runtime.accounts.list()),
        len(runtime.account_identities.list()),
        len(runtime.account_sessions.list()),
        len(runtime.memberships.list()),
    )


def test_a_new_person_gets_an_account_with_oidc_and_email_identities(google_world):
    world = google_world()
    response = google_sign_in(world.client, world.fake, "New.Person@Example.test")
    assert response.headers["location"] == "/"
    (account,) = world.runtime.accounts.list()
    assert account.display_name == "Pat Person"  # Google's name claim
    assert account.status is AccountStatus.ACTIVE
    sub = next(i.subject for i in world.runtime.account_identities.list() if i.issuer)
    assert _identities(world) == sorted(
        [("oidc", ISSUER, sub), ("email", None, "new.person@example.test")]
    )
    assert all(i.verified_at for i in world.runtime.account_identities.list())
    me = world.client.get("/api/v0/account/browser/me").json()
    assert me["account"] == {
        "id": account.id,
        "display_name": "Pat Person",
        "role_title": None,
    }


def test_the_display_name_falls_back_to_the_email_local_part(google_world):
    world = google_world()
    google_sign_in(world.client, world.fake, "no.name@example.test", name=None)
    (account,) = world.runtime.accounts.list()
    assert account.display_name == "no.name"


def test_an_existing_account_is_found_by_its_verified_email(google_world):
    world = google_world()
    existing = world.service.find_or_create_account("owner@example.test")
    response = google_sign_in(world.client, world.fake, "Owner@Example.test")
    assert response.headers["location"] == "/"
    assert [a.id for a in world.runtime.accounts.list()] == [existing.id]
    kinds = {i.kind for i in world.runtime.account_identities.list()}
    assert kinds == {AccountIdentityKind.OIDC, AccountIdentityKind.EMAIL}
    assert {i.account_id for i in world.runtime.account_identities.list()} == {
        existing.id
    }


def test_an_unverified_email_identity_never_claims_an_account(google_world):
    world = google_world()
    planted = world.service.find_or_create_account("claimed@example.test")
    identity = world.runtime.account_identities.list()[0]
    world.runtime.account_identities.put(
        identity.model_copy(update={"verified_at": None})
    )
    google_sign_in(world.client, world.fake, "claimed@example.test")
    accounts = {a.id for a in world.runtime.accounts.list()}
    assert planted.id in accounts and len(accounts) == 2


def test_an_existing_account_is_found_by_its_oidc_subject_even_if_the_email_changes(
    google_world,
):
    world = google_world()
    google_sign_in(world.client, world.fake, "old@example.test", sub="stable-sub")
    (account,) = world.runtime.accounts.list()
    google_sign_in(world.client, world.fake, "new@example.test", sub="stable-sub")
    assert [a.id for a in world.runtime.accounts.list()] == [account.id]
    assert _identities(world) == sorted(
        [
            ("oidc", ISSUER, "stable-sub"),
            ("email", None, "old@example.test"),
            ("email", None, "new@example.test"),
        ]
    )


def test_repeat_sign_ins_add_nothing(google_world):
    world = google_world()
    google_sign_in(world.client, world.fake, EMAIL, sub="s-1")
    before = _counts(world)
    google_sign_in(world.client, world.fake, EMAIL, sub="s-1")
    after = _counts(world)
    assert after[:2] == before[:2] and after[3] == before[3]
    assert after[2] == before[2] + 1  # only a new session


def test_an_oidc_account_and_an_email_account_that_differ_are_a_conflict(google_world):
    world = google_world()
    google_sign_in(world.client, world.fake, "one@example.test", sub="sub-one")
    google_sign_in(world.client, world.fake, "two@example.test", sub="sub-two")
    world.client.cookies.clear()
    before = (_counts(world), _identities(world))
    refused = google_sign_in(
        world.client, world.fake, "two@example.test", sub="sub-one"
    )
    assert refused.headers["location"] == ERROR + "conflict"
    assert "wvs_acbr_" not in refused.headers.get("set-cookie", "")
    assert (_counts(world), _identities(world)) == before


@pytest.mark.parametrize("via", ["oidc", "email"])
def test_a_disabled_account_is_refused_and_nothing_changes(google_world, via):
    world = google_world()
    if via == "oidc":
        google_sign_in(world.client, world.fake, EMAIL, sub="dis-sub")
        (account,) = world.runtime.accounts.list()
        world.client.cookies.clear()
    else:
        account = world.service.find_or_create_account(EMAIL)
    world.runtime.accounts.put(
        account.model_copy(update={"status": AccountStatus.DISABLED})
    )
    before = (_counts(world), _identities(world))
    refused = google_sign_in(world.client, world.fake, EMAIL, sub="dis-sub")
    assert refused.headers["location"] == ERROR + "disabled"
    assert "wvs_acbr_" not in refused.headers.get("set-cookie", "")
    assert (_counts(world), _identities(world)) == before


@pytest.mark.parametrize(
    ("env", "email", "allowed"),
    [
        (
            {"WEAVES_ACCOUNT_ALLOWED_EMAILS": "boss@corp.test, Me@Corp.Test"},
            "me@corp.test",
            True,
        ),
        ({"WEAVES_ACCOUNT_ALLOWED_EMAILS": "boss@corp.test"}, "me@corp.test", False),
        (
            {"WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS": "corp.test,@other.test"},
            "x@Corp.test",
            True,
        ),
        (
            {"WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS": "corp.test,@other.test"},
            "x@other.test",
            True,
        ),
        (
            {"WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS": "corp.test"},
            "x@evilcorp.test",
            False,
        ),
        (
            {"WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS": "corp.test"},
            "x@sub.corp.test",
            False,
        ),
        (
            {
                "WEAVES_ACCOUNT_ALLOWED_EMAILS": "vip@free.test",
                "WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS": "corp.test",
            },
            "vip@free.test",
            True,
        ),
        (
            {
                "WEAVES_ACCOUNT_ALLOWED_EMAILS": "vip@free.test",
                "WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS": "corp.test",
            },
            "other@free.test",
            False,
        ),
        ({}, "anyone@anywhere.test", True),
    ],
)
def test_the_allowlist_decides_who_may_sign_in_or_create_an_account(
    google_world, env, email, allowed
):
    world = google_world(**env)
    response = google_sign_in(world.client, world.fake, email)
    if allowed:
        assert response.headers["location"] == "/"
        assert len(world.runtime.accounts.list()) == 1
    else:
        assert response.headers["location"] == ERROR + "not_allowed"
        assert _counts(world) == (0, 0, 0, 0)  # nothing is created


def test_the_allowlist_also_stops_an_account_that_already_exists(google_world):
    world = google_world(WEAVES_ACCOUNT_ALLOWED_EMAILS="someone.else@corp.test")
    world.service.find_or_create_account(EMAIL)
    before = _counts(world)
    refused = google_sign_in(world.client, world.fake, EMAIL)
    assert refused.headers["location"] == ERROR + "not_allowed"
    assert _counts(world) == before


def test_an_invited_member_is_activated_and_can_select_the_organization(google_world):
    world = google_world()
    seed_organization(world.runtime.organizations, "org-invited")
    user, principal = seed_member(
        world.runtime.users,
        world.runtime.principals,
        "org-invited",
        "Invited@Example.test",
        status=UserStatus.INVITED,
    )
    world.runtime.principals.put(
        principal.model_copy(update={"status": PrincipalStatus.DISABLED})
    )
    headers = google_headers(
        world.client, world.fake, "invited@example.test", "org-invited"
    )
    assert world.runtime.users.get(user.id).status is UserStatus.ACTIVE
    assert world.runtime.principals.get(principal.id).status is PrincipalStatus.ACTIVE
    (membership,) = world.runtime.memberships.list()
    assert (
        membership.status is MembershipStatus.ACTIVE and membership.user_id == user.id
    )
    me = world.client.get("/api/v0/account/browser/me").json()
    assert [m["organization_id"] for m in me["memberships"]] == ["org-invited"]
    account = world.runtime.accounts.list()[0]
    assert world.service.selector.select(account, "org-invited").user_id == user.id
    assert headers["X-Organization-Id"] == "org-invited"


def test_a_signed_in_member_uses_the_cookie_in_their_organization(google_world):
    world = google_world()
    headers = google_headers(world.client, world.fake, EMAIL, world.org.id)
    assert world.client.get("/api/v0/agents", headers=headers).status_code == 200
    other = {**headers, "X-Organization-Id": "someone-elses-org"}
    assert world.client.get("/api/v0/agents", headers=other).status_code == 403


def test_the_operator_bootstrapped_owner_lands_in_the_existing_organization(
    google_world, capsys
):
    world = google_world()
    status, _, _ = run_script(
        bootstrap_account_owner,
        capsys,
        world.runtime,
        "--email",
        "Owner@Weave.test",
        "--organization-name",
        "Owner Org",
    )
    assert status == 0
    (owner_account,) = [
        a for a in world.runtime.accounts.list() if a.display_name == "owner"
    ]
    organizations_before = len(world.runtime.organizations.list())
    accounts_before = len(world.runtime.accounts.list())

    response = google_sign_in(
        world.client, world.fake, "owner@weave.test", name="Owner"
    )
    assert response.headers["location"] == "/"
    assert len(world.runtime.accounts.list()) == accounts_before  # same account
    assert len(world.runtime.organizations.list()) == organizations_before
    me = world.client.get("/api/v0/account/browser/me").json()
    assert me["account"]["id"] == owner_account.id
    assert [m["organization_name"] for m in me["memberships"]] == ["Owner Org"]
    owned = [
        i
        for i in world.runtime.account_identities.list()
        if i.account_id == owner_account.id
    ]
    assert {i.kind for i in owned} == {
        AccountIdentityKind.EMAIL,
        AccountIdentityKind.OIDC,
    }
    org_id = me["memberships"][0]["organization_id"]
    headers = google_headers(world.client, world.fake, "owner@weave.test", org_id)
    assert world.client.get("/api/v0/agents", headers=headers).status_code == 200


def test_losing_a_creation_race_retries_and_leaves_no_orphans(google_world):
    world = google_world()
    identities = world.runtime.account_identities
    real_create = identities.create
    calls = []

    def flaky(record):
        calls.append(record.kind)
        if len(calls) == 2:  # the second identity of the first attempt collides
            raise ValueError("duplicate identity")
        return real_create(record)

    identities.create = flaky
    response = google_sign_in(world.client, world.fake, "racer@example.test")
    assert response.headers["location"] == "/"
    assert len(world.runtime.accounts.list()) == 1
    assert len(world.runtime.account_identities.list()) == 2
    assert (
        world.runtime.accounts.list()[0].id
        == next(iter(world.runtime.account_identities.list())).account_id
    )
