"""A verified account creates organizations atomically and idempotently."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import AccountIdentity, AccountIdentityKind
from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from modules.auth.account_organization_input import AccountOrganizationCommand
from modules.auth.account_organizations import IdempotencyConflict
from shared.clock import new_id, utc_now
from shared.errors import LoginThrottled
from tests.support.account_org_support import (
    ENABLE,
    PROVISIONED_COLLECTIONS,
    PROVISIONING_WRITES,
    URL,
    bearer,
    create_repositories,
    fail_on_write,
    prepare,
)

EMAIL = "founder@example.test"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv(ENABLE, "true")
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    signer = prepare(runtime)
    account, token = signer.sign_in(EMAIL)
    client = TestClient(create_app(runtime), raise_server_exceptions=False)
    with client:
        yield {
            "runtime": runtime,
            "signer": signer,
            "client": client,
            "account": account,
            "token": token,
            "service": runtime.account_organization_service,
        }
    runtime.close()


def _post(world, key="key-1", token=None, **body):
    body.setdefault("name", "Acme")
    return world["client"].post(
        URL, json=body, headers=bearer(token or world["token"], key)
    )


def _rows(world):
    repositories = create_repositories(world["service"])
    return {name: len(repo.list()) for name, repo in repositories.items()}


def test_creating_an_organization_provisions_one_complete_tenant(world):
    response = _post(world, timezone="Asia/Kolkata", locale="en-IN")
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"organization", "workspace", "membership"}
    assert body["organization"]["name"] == "Acme"
    assert body["organization"]["status"] == "active"
    assert "token" not in response.text and "access_token" not in response.text
    rows = _rows(world)
    assert rows["organizations"] == 1 and rows["workspaces"] == 1
    assert rows["users"] == 1 and rows["principals"] == 1
    assert rows["roles"] == 3 and rows["role_bindings"] == 1
    assert rows["api_credentials"] == 0  # the account session is the credential
    assert rows["memberships"] == 1 and rows["account_organization_requests"] == 1
    membership = body["membership"]
    assert membership["status"] == "active"
    assert membership["organization_id"] == body["organization"]["id"]


def test_owner_is_an_active_admin_and_the_audit_event_hides_the_email(world):
    body = _post(world).json()
    runtime = world["runtime"]
    org_id = body["organization"]["id"]
    user = runtime.users.get(body["membership"]["user_id"])
    principal = runtime.principals.get(body["membership"]["principal_id"])
    assert user.email == EMAIL and user.status.value == "active"
    assert principal.user_id == user.id and principal.org_id == org_id
    (binding,) = runtime.role_bindings.list()
    assert binding.principal_id == principal.id
    assert runtime.roles.get(binding.role_id).name.casefold() == "admin"
    (event,) = [e for e in runtime.audit_events.list() if e.org_id == org_id]
    assert event.action == "organization.created"
    assert event.actor_principal_id == principal.id
    assert EMAIL not in event.model_dump_json()


def test_the_creator_selects_the_organization_and_other_accounts_cannot(world):
    org_id = _post(world).json()["organization"]["id"]
    client = world["client"]
    own = client.get("/api/v0/agents", headers=bearer(world["token"], org=org_id))
    assert own.status_code == 200, own.text
    _, stranger = world["signer"].sign_in("stranger@example.test")
    denied = client.get("/api/v0/agents", headers=bearer(stranger, org=org_id))
    assert denied.status_code == 403
    me = client.get("/api/v0/account/me", headers=bearer(stranger))
    assert me.json()["memberships"] == []
    mine = client.get("/api/v0/account/me", headers=bearer(world["token"]))
    assert [m["organization_id"] for m in mine.json()["memberships"]] == [org_id]


def test_the_same_key_and_body_replays_the_same_organization(world):
    first = _post(world)
    second = _post(world)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert _rows(world)["organizations"] == 1


def test_the_same_key_with_a_different_body_conflicts(world):
    assert _post(world).status_code == 201
    changed = _post(world, name="Other name")
    assert changed.status_code == 409
    assert _rows(world)["organizations"] == 1
    assert _post(world, key="key-2", name="Other name").status_code == 201


def test_keys_are_scoped_to_the_account(world):
    _, other = world["signer"].sign_in("second@example.test")
    first = _post(world, key="shared")
    second = _post(world, key="shared", token=other)
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["organization"]["id"] != second.json()["organization"]["id"]


def test_concurrent_identical_requests_create_exactly_one_organization(world):
    account, service = world["account"], world["service"]
    command = AccountOrganizationCommand.build(name="Racing")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: service.create(account, command, "same-key", "203.0.113.1"),
                range(8),
            )
        )
    assert len({r.organization.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert _rows(world)["organizations"] == 1


def test_the_per_account_cap_counts_idempotency_records(world, monkeypatch):
    monkeypatch.setenv("WEAVES_ACCOUNT_ORGANIZATION_LIMIT", "2")
    assert _post(world, key="a", name="One").status_code == 201
    assert _post(world, key="b", name="Two").status_code == 201
    capped = _post(world, key="c", name="Three")
    assert capped.status_code == 403
    assert "limit" in capped.json()["detail"]
    assert _post(world, key="a", name="One").status_code == 200  # replay still works
    assert _rows(world)["organizations"] == 2


def test_the_default_cap_is_three(world):
    codes = [_post(world, key=f"k{i}", name=f"Org {i}").status_code for i in range(4)]
    assert codes == [201, 201, 201, 403]


def test_creation_is_off_unless_enabled(world, monkeypatch):
    monkeypatch.delenv(ENABLE)
    assert _post(world).status_code == 404
    monkeypatch.setenv(ENABLE, "false")
    assert _post(world).status_code == 404
    assert _rows(world)["organizations"] == 0


def test_the_owner_email_must_be_a_verified_identity_of_the_caller(world):
    runtime, account = world["runtime"], world["account"]
    now = utc_now()
    runtime.account_identities.create(
        AccountIdentity(
            id=new_id(),
            account_id=account.id,
            kind=AccountIdentityKind.EMAIL,
            subject="unverified@example.test",
            created_at=now,
            updated_at=now,
        )
    )
    world["signer"].sign_in("someone.else@example.test")
    for email in ("unverified@example.test", "someone.else@example.test"):
        refused = _post(world, owner_email=email)
        assert refused.status_code == 422
    assert _rows(world)["organizations"] == 0
    assert _post(world, key="ok", owner_email=EMAIL.upper()).status_code == 201


def test_the_default_owner_email_is_the_earliest_verified_one(world):
    runtime, account = world["runtime"], world["account"]
    later = utc_now()
    runtime.account_identities.create(
        AccountIdentity(
            id=new_id(),
            account_id=account.id,
            kind=AccountIdentityKind.EMAIL,
            subject="aaa.later@example.test",
            verified_at=later,
            created_at=later,
            updated_at=later,
        )
    )
    body = _post(world).json()
    assert runtime.users.get(body["membership"]["user_id"]).email == EMAIL
    again = _post(world, key="two", owner_email="aaa.later@example.test").json()
    assert (
        runtime.users.get(again["membership"]["user_id"]).email
        == "aaa.later@example.test"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x" * 161},
        {"name": "Bad\nname"},
        {"name": "Ok", "timezone": "Mars/Phobos"},
        {"name": "Ok", "timezone": "../etc/passwd"},
        {"name": "Ok", "locale": "not a locale"},
        {"name": "Ok", "unknown": 1},
    ],
)
def test_invalid_input_is_refused_before_anything_is_written(world, body):
    response = world["client"].post(
        URL, json=body, headers=bearer(world["token"], "key-x")
    )
    assert response.status_code == 422
    assert _rows(world)["organizations"] == 0


def test_the_idempotency_key_is_required_and_bounded(world):
    client, token = world["client"], world["token"]
    missing = client.post(URL, json={"name": "Acme"}, headers=bearer(token))
    assert missing.status_code == 422
    long = client.post(URL, json={"name": "Acme"}, headers=bearer(token, "k" * 129))
    assert long.status_code == 422
    assert _rows(world)["organizations"] == 0


def test_the_route_requires_an_account_session(world):
    anonymous = world["client"].post(
        URL, json={"name": "Acme"}, headers={"Idempotency-Key": "k"}
    )
    assert anonymous.status_code == 401


def test_a_disabled_account_cannot_create(world):
    service, account = world["service"], world["account"]
    disabled = account.model_copy(update={"status": "disabled"})
    with pytest.raises(PermissionError):
        service.create(disabled, AccountOrganizationCommand.build(name="X"), "k", "ip")


def test_attempts_are_throttled_per_account_and_per_source(world):
    service, account = world["service"], world["account"]
    command = AccountOrganizationCommand.build(name="Throttled")
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=2)
    service.create(account, command, "k", "203.0.113.5")
    service.create(account, command, "k", "203.0.113.6")
    with pytest.raises(LoginThrottled):
        service.create(account, command, "k", "203.0.113.7")
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=2)
    service.create(account, command, "k", "203.0.113.9")
    other, _ = world["signer"].sign_in("other.person@example.test")
    service.create(other, command, "k", "203.0.113.9")
    with pytest.raises(LoginThrottled):
        service.create(
            world["signer"].sign_in("third@example.test")[0],
            command,
            "k",
            "203.0.113.9",
        )


def test_service_errors_map_to_distinct_exceptions(world):
    service, account = world["service"], world["account"]
    service.create(account, AccountOrganizationCommand.build(name="A"), "k", "ip")
    with pytest.raises(IdempotencyConflict):
        service.create(account, AccountOrganizationCommand.build(name="B"), "k", "ip")


def test_provisioning_writes_every_expected_row_exactly_once(world, monkeypatch):
    state = fail_on_write(monkeypatch, world["service"], 10_000)
    assert _post(world).status_code == 201
    assert state["calls"] == PROVISIONING_WRITES


@pytest.mark.parametrize("step", range(1, PROVISIONING_WRITES + 1))
def test_a_failure_at_any_step_leaves_no_partial_tenant_and_a_retry_succeeds(
    world, monkeypatch, step
):
    with monkeypatch.context() as patch:
        fail_on_write(patch, world["service"], step)
        failed = _post(world)
        assert failed.status_code == 500
    assert {name: count for name, count in _rows(world).items() if count} == {}
    assert set(PROVISIONED_COLLECTIONS) >= set(_rows(world))
    retry = _post(world)
    assert retry.status_code == 201, retry.text
    assert _rows(world)["organizations"] == 1
    assert _post(world).status_code == 200
