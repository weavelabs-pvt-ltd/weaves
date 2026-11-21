"""Operator-only VM scripts: bootstrap the account owner, break-glass sessions."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import (
    AccountIdentityKind,
    AccountStatus,
    MembershipStatus,
    PrincipalStatus,
    UserStatus,
)
from tests.support.operator_scripts import (
    bootstrap_account_owner,
    issue_account_session,
    run_script,
)

EMAIL = "Owner@Example.Test"
NAME = "Weaves Operator Org"


@pytest.fixture
def runtime():
    active = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    active.bootstrap()
    yield active
    active.close()


def _bootstrap(capsys, runtime, email=EMAIL, name=NAME):
    return run_script(
        bootstrap_account_owner,
        capsys,
        runtime,
        "--email",
        email,
        "--organization-name",
        name,
    )


def _snapshot(runtime):
    repositories = runtime._repository_set._repositories
    return {
        name: sorted(item.model_dump_json() for item in repository.list())
        for name, repository in repositories.items()
        if hasattr(repository, "list")
    }


def test_bootstrap_creates_account_verified_identity_org_owner_and_membership(
    runtime, capsys
):
    status, out, logs = _bootstrap(capsys, runtime)
    assert status == 0 and out == ""
    (account,) = runtime.accounts.list()
    (identity,) = runtime.account_identities.list()
    assert identity.account_id == account.id
    assert identity.kind is AccountIdentityKind.EMAIL
    assert identity.subject == "owner@example.test" and identity.verified_at
    assert account.status is AccountStatus.ACTIVE
    (organization,) = runtime.organizations.list()
    assert organization.name == NAME
    (user,) = runtime.users.list()
    (principal,) = runtime.principals.list()
    assert user.status is UserStatus.ACTIVE
    assert principal.status is PrincipalStatus.ACTIVE
    assert user.email == "owner@example.test"
    assert not runtime.api_credentials.list()
    (membership,) = runtime.memberships.list()
    assert membership.status is MembershipStatus.ACTIVE
    assert (membership.account_id, membership.org_id) == (account.id, organization.id)
    assert membership.user_id == user.id and membership.principal_id == principal.id
    (line,) = logs
    assert line == {
        "event": "operator.bootstrap_account_owner",
        "account_id": account.id,
        "organization_id": organization.id,
        "created": True,
    }


def test_rerunning_the_bootstrap_changes_nothing(runtime, capsys):
    _bootstrap(capsys, runtime)
    before = _snapshot(runtime)
    status, _, logs = _bootstrap(capsys, runtime)
    again, _, more = _bootstrap(capsys, runtime, email=" owner@example.test ")
    assert status == again == 0
    assert logs[0]["created"] is False and more[0]["created"] is False
    assert _snapshot(runtime) == before


def test_bootstrap_after_the_person_already_signed_in_does_not_duplicate(
    runtime, capsys
):
    _bootstrap(capsys, runtime)
    organization = runtime.organizations.list()[0]
    # Forget the idempotency record: the name check alone must prevent a duplicate.
    for record in runtime.account_organization_requests.list():
        runtime.account_organization_requests.delete(record.id)
    before = _snapshot(runtime)
    status, _, logs = _bootstrap(capsys, runtime)
    assert status == 0
    assert logs[0]["organization_id"] == organization.id and not logs[0]["created"]
    assert _snapshot(runtime) == before


def test_bootstrap_refuses_invalid_input_and_creates_nothing(runtime, capsys):
    for email, name in (("not-an-email", NAME), (EMAIL, "  ")):
        status, _, logs = _bootstrap(capsys, runtime, email=email, name=name)
        assert status == 2
        assert logs[0]["event"] == "operator.bootstrap_account_owner.refused"
    assert not runtime.accounts.list() and not runtime.organizations.list()


def test_the_bootstrapped_owner_can_use_an_issued_session_over_http(runtime, capsys):
    _bootstrap(capsys, runtime)
    status, out, logs = run_script(
        issue_account_session,
        capsys,
        runtime,
        "--email",
        EMAIL,
        "--minutes",
        "30",
    )
    token = out.strip()
    assert status == 0 and token.startswith("wvs_acct_") and out.count("\n") == 1
    assert token not in json.dumps(logs)
    (line,) = logs
    assert set(line) == {"event", "account_id", "session_id", "expires_at"}
    session = runtime.account_sessions.get(line["session_id"])
    assert (session.expires_at - session.created_at).total_seconds() == 30 * 60
    organization = runtime.organizations.list()[0]
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v0/account/me", headers=headers).status_code == 200
        tenant = {**headers, "X-Organization-Id": organization.id}
        me = client.get("/api/v0/auth/me", headers=tenant)
        assert me.status_code == 200, me.text
        assert "users.manage" in me.json()["effective_permissions"]


def test_issue_refuses_unknown_and_disabled_accounts_and_bad_lifetimes(runtime, capsys):
    refused = []
    for args in (
        ("--email", "nobody@example.test"),
        ("--email", EMAIL, "--minutes", "0"),
        ("--email", EMAIL, "--minutes", "1441"),
    ):
        refused.append(run_script(issue_account_session, capsys, runtime, *args))
    _bootstrap(capsys, runtime)
    account = runtime.accounts.list()[0]
    runtime.accounts.put(account.model_copy(update={"status": AccountStatus.DISABLED}))
    refused.append(run_script(issue_account_session, capsys, runtime, "--email", EMAIL))
    for status, out, logs in refused:
        assert status == 2 and out == ""
        assert logs[0]["event"] == "operator.issue_account_session.refused"
    assert not runtime.account_sessions.list()


def test_the_scripts_are_not_reachable_over_http(runtime):
    with TestClient(create_app(runtime)) as client:
        paths = {getattr(route, "path", "") for route in client.app.routes}
    assert any(path.startswith("/api/v0/account/") for path in paths)
    assert not [p for p in paths if "bootstrap" in p or "break" in p or "operator" in p]
