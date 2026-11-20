"""Attaching another email requires proof and never merges or steals accounts."""

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from tests.support.account_world import (
    HMAC_KEY,
    CapturedMail,
    seed_member,
    seed_organization,
)
from tests.support.runtime import LocalPlatformRuntime

FIRST = "first.person@example.test"
SECOND = "second.person@example.test"


@pytest.fixture
def world():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    service = runtime.account_sign_in_service
    mail = CapturedMail()
    service.email_sender = mail
    service.email_login_hmac_key = HMAC_KEY
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=50)
    for org in ("org-a", "org-b"):
        seed_organization(runtime.organizations, org)
    seed_member(runtime.users, runtime.principals, "org-a", FIRST)
    seed_member(runtime.users, runtime.principals, "org-b", SECOND)
    client = TestClient(create_app(runtime), raise_server_exceptions=False)
    with client:
        yield runtime, service, mail, client
    runtime.close()


def _sign_in(service, mail, email, source="198.51.100.1"):
    challenge = service.start_email_login(email, source)
    return service.verify_email_login(challenge, mail.code(), source)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _start(client, token, email):
    return client.post(
        "/api/v0/account/identities/email-code",
        headers=_auth(token),
        json={"email": email},
    )


def _confirm(client, token, challenge_id, mail):
    return client.post(
        "/api/v0/account/identities/email-verify",
        headers=_auth(token),
        json={"challenge_id": challenge_id, "code": mail.code()},
    )


def test_proven_second_email_joins_the_account_and_links_its_memberships(world):
    runtime, service, mail, client = world
    account, _, token = _sign_in(service, mail, FIRST)
    assert {m.org_id for m in runtime.memberships.list()} == {"org-a"}

    started = _start(client, token, SECOND)
    assert started.status_code == 202
    done = _confirm(client, token, started.json()["challenge_id"], mail)

    assert done.status_code == 200, done.text
    assert {m["organization_id"] for m in done.json()["memberships"]} == {
        "org-a",
        "org-b",
    }
    assert len(done.json()["identities"]) == 2
    assert all(m.account_id == account.id for m in runtime.memberships.list())


def test_an_address_verified_on_another_account_is_never_moved_or_merged(world):
    runtime, service, mail, client = world
    _, _, first_token = _sign_in(service, mail, FIRST)
    other, _, _ = _sign_in(service, mail, SECOND, source="198.51.100.2")
    before = {m.id: m.account_id for m in runtime.memberships.list()}

    started = _start(client, first_token, SECOND)
    refused = _confirm(client, first_token, started.json()["challenge_id"], mail)

    assert refused.status_code == 409
    assert {m.id: m.account_id for m in runtime.memberships.list()} == before
    assert (
        runtime.account_sign_in_service.identities.find_by_field(
            "subject", SECOND
        ).account_id
        == other.id
    )


def test_a_wrong_or_foreign_proof_changes_nothing(world):
    runtime, service, mail, client = world
    _, _, token_a = _sign_in(service, mail, FIRST)
    _, _, token_b = _sign_in(service, mail, SECOND, source="198.51.100.2")
    started = _start(client, token_a, "third.person@example.test")
    wrong = client.post(
        "/api/v0/account/identities/email-verify",
        headers=_auth(token_a),
        json={"challenge_id": started.json()["challenge_id"], "code": "000000"},
    )
    assert wrong.status_code == 400
    stolen = _confirm(client, token_b, started.json()["challenge_id"], mail)
    assert stolen.status_code == 400  # another account cannot redeem it
    assert len(runtime.account_sign_in_service.identities.list()) == 2


def test_an_add_email_code_cannot_sign_anyone_in_and_vice_versa(world):
    _, service, mail, client = world
    _, _, token = _sign_in(service, mail, FIRST)
    started = _start(client, token, "third.person@example.test")
    with pytest.raises(PermissionError):
        service.verify_email_login(started.json()["challenge_id"], mail.code())
    sign_in = service.start_email_login(SECOND, "198.51.100.9")
    verify = client.post(
        "/api/v0/account/identities/email-verify",
        headers=_auth(token),
        json={"challenge_id": sign_in, "code": mail.code()},
    )
    assert verify.status_code == 400


def test_removing_an_email_keeps_one_verified_identity(world):
    _, service, mail, client = world
    _, _, token = _sign_in(service, mail, FIRST)
    started = _start(client, token, SECOND)
    _confirm(client, token, started.json()["challenge_id"], mail)
    identities = client.get("/api/v0/account/me", headers=_auth(token)).json()[
        "identities"
    ]
    assert len(identities) == 2
    ids = [i["id"] for i in identities]

    assert (
        client.delete(
            f"/api/v0/account/identities/{ids[0]}", headers=_auth(token)
        ).status_code
        == 204
    )
    last = client.delete(f"/api/v0/account/identities/{ids[1]}", headers=_auth(token))
    assert last.status_code == 409
    assert (
        client.delete(
            "/api/v0/account/identities/not-mine", headers=_auth(token)
        ).status_code
        == 404
    )


def test_another_accounts_identity_cannot_be_removed(world):
    _, service, mail, client = world
    _, _, token_a = _sign_in(service, mail, FIRST)
    _, _, token_b = _sign_in(service, mail, SECOND, source="198.51.100.2")
    victim = client.get("/api/v0/account/me", headers=_auth(token_b)).json()[
        "identities"
    ][0]["id"]
    assert (
        client.delete(
            f"/api/v0/account/identities/{victim}", headers=_auth(token_a)
        ).status_code
        == 404
    )


def test_identity_routes_need_an_account_session(world):
    _, _, _, client = world
    for method, path in (
        ("post", "/api/v0/account/identities/email-code"),
        ("post", "/api/v0/account/identities/email-verify"),
        ("delete", "/api/v0/account/identities/x"),
    ):
        assert client.request(method, path, json={}).status_code == 401
