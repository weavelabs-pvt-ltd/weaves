"""Operator scripts run on the owner connection; the restricted role then serves."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import AccountStatus
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from shared.request_context import require_tenant_context, system_scope
from tests.support.operator_scripts import (
    bootstrap_account_owner,
    issue_account_session,
    run_script,
)

EMAIL = "pg.operator@example.test"
NAME = "Operator PG Org"


@pytest.fixture
def stack(isolated_postgres_url):
    owner_store = ProductPostgresStore(isolated_postgres_url, app_role="")
    owner = compose_platform_runtime(
        http_client=httpx.Client(), store=owner_store, seed_local_defaults=False
    )
    owner.bootstrap()
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=4)
    restricted_store = ProductPostgresStore(pool=pool, app_role="weaves_app")
    restricted = compose_platform_runtime(
        http_client=httpx.Client(), store=restricted_store, seed_local_defaults=False
    )
    restricted.bootstrap()
    require_tenant_context(True)  # production posture: no implicit local tenant
    yield owner, restricted, owner_store
    require_tenant_context(False)
    restricted.close()
    owner.close()


def _counts(store):
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT collection, count(*) FROM product_records GROUP BY collection"
        ).fetchall()
    return dict(rows)


def _bootstrap(capsys, owner):
    return run_script(
        bootstrap_account_owner,
        capsys,
        owner,
        "--email",
        EMAIL,
        "--organization-name",
        NAME,
    )


def test_bootstrap_is_idempotent_and_leaves_no_api_credential(stack, capsys):
    owner, _, store = stack
    status, _, logs = _bootstrap(capsys, owner)
    assert status == 0 and logs[0]["created"] is True
    first = _counts(store)
    assert first.get("memberships") == 1 and first.get("organizations") == 1
    assert first.get("api_credentials", 0) == 0
    assert first.get("account_identities") == 1 and first.get("accounts") == 1
    status, _, logs = _bootstrap(capsys, owner)
    assert status == 0 and logs[0]["created"] is False
    assert _counts(store) == first


def test_issued_session_works_through_the_restricted_role(stack, capsys):
    owner, restricted, _ = stack
    _bootstrap(capsys, owner)
    status, out, logs = run_script(
        issue_account_session, capsys, owner, "--email", EMAIL, "--minutes", "15"
    )
    token = out.strip()
    assert status == 0 and token.startswith("wvs_acct_") and token not in str(logs)
    with system_scope("account"):
        organization_id = owner.memberships.list()[0].org_id
    with TestClient(create_app(restricted), raise_server_exceptions=False) as client:
        bearer = {"Authorization": f"Bearer {token}"}
        listing = client.get("/api/v0/account/me", headers=bearer)
        assert listing.status_code == 200, listing.text
        tenant = {**bearer, "X-Organization-Id": organization_id}
        me = client.get("/api/v0/auth/me", headers=tenant)
        assert me.status_code == 200, me.text
        assert me.json()["user"]["status"] == "active"
        assert client.get("/api/v0/workspaces", headers=tenant).status_code == 200
        other = {**bearer, "X-Organization-Id": "someone-elses-org"}
        assert client.get("/api/v0/auth/me", headers=other).status_code == 403


def test_issue_refuses_unknown_and_disabled_accounts(stack, capsys):
    owner, _, store = stack
    status, out, _ = run_script(
        issue_account_session, capsys, owner, "--email", "ghost@example.test"
    )
    assert status == 2 and out == ""
    _bootstrap(capsys, owner)
    with system_scope("account"):
        account = owner.accounts.list()[0]
        owner.accounts.put(
            account.model_copy(update={"status": AccountStatus.DISABLED})
        )
    status, out, _ = run_script(issue_account_session, capsys, owner, "--email", EMAIL)
    assert status == 2 and out == ""
    assert _counts(store).get("account_sessions", 0) == 0
