"""Owner export: authority, isolation, secret exclusion, size bound and audit."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from modules.auth.organization_export_rules import EXPORT_COLLECTIONS
from shared.request_context import reset_request_principal, set_request_identity
from tests.support.lifecycle_http import EXPORT, REQUEST, populated_world
from tests.support.lifecycle_world import SKILL
from tests.support.organization_administration import headers

REGISTRY = Path(__file__).resolve().parents[2] / "tenancy_harness/ownership.json"


@pytest.fixture
def world():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    world = populated_world(runtime)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        world["client"] = client
        yield world
    runtime.close()


def export(world, **kwargs):
    return world["client"].post(EXPORT, headers=headers(world, **kwargs))


def test_every_exported_collection_is_tenant_scoped_in_the_registry():
    registry = json.loads(REGISTRY.read_text())["collections"]
    for name in EXPORT_COLLECTIONS:
        assert registry[name]["scope"] in {"organization", "workspace"}, name
        assert "org_id" in registry[name]["scope_keys"], name


def test_export_is_an_uncached_attachment_without_secrets_or_other_tenants(world):
    response = export(world)
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    body = response.text
    document = json.loads(body)
    assert document["organization"]["name"] == "Org A"
    assert "ALPHA" in body and "BRAVO" not in body
    assert "owner-b@example.test" not in body
    for secret in world["seed_a"]["secrets"] + world["seed_b"]["secrets"]:
        assert secret not in body, secret[:12]
    assert "auth_ref" not in body and "secret_ref" not in body
    assert "token_digest" not in body and "secret_digest" not in body
    assert "ABCD" in body  # provider credential_last4 is allowed
    assert set(document["data"]) == set(EXPORT_COLLECTIONS)
    for membership in document["data"]["memberships"]:
        assert "account_id" not in membership
    for row in document["data"]["users"]:
        assert set(row) <= {
            "id",
            "display_name",
            "email",
            "status",
            "created_at",
            "updated_at",
        }
    for rows in document["data"].values():
        assert all(r.get("org_id", world["a"][0].id) == world["a"][0].id for r in rows)
    events = world["runtime"].audit_events.list_scoped(world["a"][0].id)
    assert [e.action for e in events].count("organization.exported") == 1


def test_only_the_owner_may_export(world):
    for token, tenant in (("member_token", "a"), ("b_token", "a")):
        assert export(world, token=token, tenant=tenant).status_code == 403
    service = world["runtime"].organization_lifecycle_service
    assert service.state_of(world["a"][0].id).value == "active"
    assert world["client"].post(EXPORT).status_code in (401, 403)


def test_export_is_allowed_while_deletion_is_pending(world):
    world["client"].post(
        REQUEST, headers=headers(world), json={"confirm_name": "Org A"}
    )
    response = export(world)
    assert response.status_code == 200
    assert json.loads(response.text)["lifecycle"]["state"] == "deletion_requested"


def test_oversized_export_is_refused_with_413(world):
    world["runtime"].organization_export_service.max_bytes = 2000
    response = export(world)
    assert response.status_code == 413
    assert "limit" in response.json()["detail"]
    events = world["runtime"].audit_events.list_scoped(world["a"][0].id)
    assert not any(e.action == "organization.exported" for e in events)


def test_other_members_private_content_is_withheld_but_the_owners_is_included(world):
    runtime, a = world["runtime"], world["a"]
    token = set_request_identity(world["member_a"][1].id, a[0].id, a[1].id)
    try:
        runtime.skills_service.create_personal_skill(
            {"SKILL.md": SKILL.format(name="skill-member-private")}
        )
    finally:
        reset_request_principal(token)
    document = json.loads(export(world).text)
    names = json.dumps(document["data"]["skill_definitions"])
    assert "skill-alpha" in names and "skill-member-private" not in names
    assert document["withheld"]["personal_skills"] == 1
