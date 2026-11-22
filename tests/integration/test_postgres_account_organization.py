"""Account-created organizations are atomic under the restricted database role."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import AccountOrganizationRequest
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.auth.account_organization_input import AccountOrganizationCommand
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
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

EMAIL = "pg.founder@example.test"


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv(ENABLE, "true")
    owner = ProductPostgresStore(isolated_postgres_url)
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=8)
    restricted = ProductPostgresStore(pool=pool, app_role="weaves_app")
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=restricted, seed_local_defaults=False
    )
    runtime.bootstrap()
    signer = prepare(runtime)
    require_tenant_context(True)  # production posture: no implicit local tenant
    with system_scope("account"):
        account, token = signer.sign_in(EMAIL)
        _, stranger = signer.sign_in("pg.stranger@example.test")
    client = TestClient(create_app(runtime), raise_server_exceptions=False)
    with client:
        yield {
            "runtime": runtime,
            "owner": owner,
            "restricted": restricted,
            "client": client,
            "account": account,
            "token": token,
            "stranger": stranger,
            "service": runtime.account_organization_service,
        }
    require_tenant_context(False)
    runtime.close()
    owner.close()


def _counts(world):
    """Rows per provisioned collection, read as the table owner (no policies)."""
    with world["owner"].connection() as connection:
        rows = connection.execute(
            "SELECT collection, count(*) FROM product_records "
            "WHERE collection = ANY(%s) GROUP BY collection",
            (list(PROVISIONED_COLLECTIONS),),
        ).fetchall()
    return {name: count for name, count in rows}


def _post(world, key="pg-key", token=None, **body):
    body.setdefault("name", "Acme PG")
    return world["client"].post(
        URL, json=body, headers=bearer(token or world["token"], key)
    )


def test_one_transaction_writes_tenant_account_and_idempotency_rows(world):
    response = _post(world)
    assert response.status_code == 201, response.text
    org_id = response.json()["organization"]["id"]
    counts = _counts(world)
    assert counts == {
        "organizations": 1,
        "organization_settings": 1,
        "workspaces": 1,
        "users": 1,
        "principals": 1,
        "roles": 3,
        "role_bindings": 1,
        "model_providers": 1,
        "model_profiles": 1,
        "approval_policies": 1,
        "audit_events": counts["audit_events"],
        "memberships": 1,
        "account_organization_requests": 1,
    }
    assert "api_credentials" not in counts
    with world["owner"].connection() as connection:
        scopes = dict(
            connection.execute(
                "SELECT collection, org_id FROM product_records "
                "WHERE collection IN ('users', 'memberships', "
                "'account_organization_requests')"
            ).fetchall()
        )
    assert scopes["users"] == org_id and scopes["memberships"] == org_id
    assert scopes["account_organization_requests"] is None  # global, account scope


def test_replay_conflict_and_per_account_keys_hold_in_postgres(world):
    first = _post(world)
    replay = _post(world)
    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json() == first.json()
    assert _post(world, name="Changed").status_code == 409
    other = _post(world, token=world["stranger"])
    assert other.status_code == 201
    assert other.json()["organization"]["id"] != first.json()["organization"]["id"]
    assert _counts(world)["organizations"] == 2


def test_the_creator_selects_the_organization_and_other_accounts_cannot(world):
    org_id = _post(world).json()["organization"]["id"]
    client = world["client"]
    own = client.get("/api/v0/agents", headers=bearer(world["token"], org=org_id))
    assert own.status_code == 200, own.text
    denied = client.get("/api/v0/agents", headers=bearer(world["stranger"], org=org_id))
    assert denied.status_code == 403
    other = client.get("/api/v0/account/me", headers=bearer(world["stranger"]))
    assert other.json()["memberships"] == []


def test_concurrent_identical_requests_create_exactly_one_organization(world):
    account, service = world["account"], world["service"]
    command = AccountOrganizationCommand.build(name="Racing PG")

    def attempt(_):
        with system_scope("account"):
            return service.create(account, command, "race", "203.0.113.1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert len({r.organization.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    counts = _counts(world)
    assert counts["organizations"] == 1 and counts["account_organization_requests"] == 1


def test_the_cap_applies_to_distinct_keys_even_when_concurrent(world, monkeypatch):
    monkeypatch.setenv("WEAVES_ACCOUNT_ORGANIZATION_LIMIT", "2")
    account, service = world["account"], world["service"]

    def attempt(index):
        command = AccountOrganizationCommand.build(name=f"Org {index}")
        try:
            return service.create(account, command, f"key-{index}", "203.0.113.2")
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert sum(not isinstance(o, Exception) for o in outcomes) == 2
    assert _counts(world)["organizations"] == 2


def test_account_rows_are_invisible_to_tenants_and_the_database_gates_writes(world):
    org_id = _post(world).json()["organization"]["id"]
    restricted = world["restricted"]

    def visible():
        with restricted.connection() as connection:
            return connection.execute(
                "SELECT count(*) FROM product_records "
                "WHERE collection = 'account_organization_requests'"
            ).fetchone()[0]

    token = set_request_identity("principal", org_id, "ws")
    try:
        assert visible() == 0
    finally:
        reset_request_principal(token)
    with system_scope("auth"):
        assert visible() == 0
    with system_scope("account"):
        assert visible() == 1
    repository = world["service"]._requests
    with pytest.raises(psycopg.Error):  # no account scope: write refused
        repository.create(
            AccountOrganizationRequest(
                id="a" * 64,
                account_id="acct",
                key_digest="b" * 64,
                request_fingerprint="c" * 64,
                organization_id="x",
                created_at=world["account"].created_at,
            )
        )


def test_the_database_refuses_a_duplicate_account_key_pair(world):
    assert _post(world).status_code == 201
    with world["owner"].connection() as connection:
        payload = connection.execute(
            "SELECT payload FROM product_records "
            "WHERE collection = 'account_organization_requests'"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO product_records (collection, record_id, payload) "
                "VALUES ('account_organization_requests', 'other-id', %s)",
                (psycopg.types.json.Jsonb(payload),),
            )


@pytest.mark.parametrize("step", range(1, PROVISIONING_WRITES + 1))
def test_a_failure_at_any_step_rolls_back_everything_and_a_retry_succeeds(
    world, monkeypatch, step
):
    baseline = _counts(world)
    with monkeypatch.context() as patch:
        state = fail_on_write(patch, world["service"], step)
        failed = _post(world)
        assert failed.status_code == 500 and state["calls"] == step
        assert failed.headers["x-request-id"] == failed.json()["request_id"]
    assert _counts(world) == baseline, f"partial tenant after failure at write {step}"
    assert "api_credentials" not in _counts(world)
    retry = _post(world)
    assert retry.status_code == 201, retry.text
    assert _counts(world)["organizations"] == 1
    assert _post(world).status_code == 200
    assert set(create_repositories(world["service"])) == set(PROVISIONED_COLLECTIONS)
