"""``GET /api/v0/usage``: organization-scoped, Owner/Admin only, bounded."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from clients.model_providers.gateway import ModelGateway
from contracts.v1 import ModelProviderType, ModelResponse, ModelUsage
from shared.clock import utc_now
from shared.request_context import reset_request_principal, set_request_identity
from tests.support.account_session import account_headers
from tests.support.organizations import onboard
from tests.support.runtime import LocalPlatformRuntime

USAGE = "/api/v0/usage"


class Model:
    def complete(self, request, binding):
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content="reply",
            usage=ModelUsage(input_tokens=30, output_tokens=12, total_tokens=42),
        )


@pytest.fixture
def world():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    runtime.model_adapters[ModelProviderType.LOCAL] = Model()
    runtime.agent_execution_service.model_gateway = ModelGateway(runtime.model_adapters)
    with TestClient(create_app(runtime)) as client:
        yield _world(runtime, client)


def _world(runtime, client):
    org = onboard(
        client,
        name="Usage org",
        owner_email="usage-owner@example.test",
        owner_display_name="Owner",
    )
    workspace = org["workspace"]["id"]
    owner = {
        "Authorization": f"Bearer {org['access_token']}",
        "X-Workspace-ID": workspace,
    }
    roles = client.get("/api/v0/roles", headers=owner).json()
    user_role = next(r for r in roles if r["system_role_key"] == "user")
    member = client.post(
        "/api/v0/users",
        headers=owner,
        json={
            "email": "usage-user@example.test",
            "display_name": "Member",
            "role_id": user_role["id"],
        },
    ).json()["user"]
    member_headers = account_headers(
        client, member["email"], member["org_id"], workspace
    )
    return {
        "client": client,
        "runtime": runtime,
        "owner": owner,
        "member": member_headers,
        "org_id": org["organization"]["id"],
        "workspace": workspace,
    }


def run_once(world):
    runtime = world["runtime"]
    principal = runtime.principals.list_scoped(world["org_id"])[0].id
    token = set_request_identity(principal, world["org_id"], world["workspace"])
    try:
        if "agent" not in world:
            world["agent"] = runtime.agent_service.create_agent(
                name="Usage agent", instructions="Reply briefly."
            )
        return runtime.run_agent(
            "hello",
            requested_by_principal_id=principal,
            agent_id=world["agent"].id,
        )
    finally:
        reset_request_principal(token)


def test_owner_reads_the_organizations_own_usage(world):
    client = world["client"]
    empty = client.get(USAGE, headers=world["owner"])
    assert empty.status_code == 200
    assert empty.json()["totals"]["calls"] == 0
    assert empty.headers["cache-control"] == "no-store"
    first = run_once(world)
    run_once(world)
    body = client.get(USAGE, headers=world["owner"]).json()
    assert body["group_by"] == "day"
    assert body["totals"]["calls"] == 2
    assert (
        body["totals"]["input_tokens"] == 60 and body["totals"]["output_tokens"] == 24
    )
    assert body["totals"]["total_tokens"] == 84
    assert len(body["groups"]) == 1 and body["groups"][0]["calls"] == 2
    assert body["unknown_calls"] == 0 and body["in_flight_calls"] == 0
    by_agent = client.get(f"{USAGE}?group_by=agent", headers=world["owner"]).json()
    assert [g["key"] for g in by_agent["groups"]] == [first.agent_run.agent_id]
    by_model = client.get(f"{USAGE}?group_by=model", headers=world["owner"]).json()
    assert by_model["totals"] == body["totals"]


def test_another_organization_sees_nothing_of_it(world):
    run_once(world)
    other = onboard(
        world["client"],
        name="Other org",
        owner_email="other-owner@example.test",
        owner_display_name="Other",
    )
    headers = {
        "Authorization": f"Bearer {other['access_token']}",
        "X-Workspace-ID": other["workspace"]["id"],
    }
    body = world["client"].get(USAGE, headers=headers).json()
    assert body["totals"]["calls"] == 0 and body["groups"] == []


def test_a_plain_member_is_refused(world):
    response = world["client"].get(USAGE, headers=world["member"])
    assert response.status_code == 403


def test_bad_parameters_are_rejected_not_ignored(world):
    client, owner = world["client"], world["owner"]
    today = utc_now().date()
    assert client.get(f"{USAGE}?bogus=1", headers=owner).status_code == 422
    assert client.get(f"{USAGE}?group_by=user", headers=owner).status_code == 422
    assert client.get(f"{USAGE}?from=yesterday", headers=owner).status_code == 422
    too_long = today - timedelta(days=92)
    assert client.get(f"{USAGE}?from={too_long}", headers=owner).status_code == 422
    exact = today - timedelta(days=91)
    assert (
        client.get(f"{USAGE}?from={exact}&to={today}", headers=owner).status_code == 200
    )
    assert (
        client.get(f"{USAGE}?from={today}&to={exact}", headers=owner).status_code == 422
    )
