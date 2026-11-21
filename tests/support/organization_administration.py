"""Reusable two-tenant ownership fixtures for HTTP and PostgreSQL races."""

from contextlib import contextmanager

from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.account_session import sign_in
from tests.support.organizations import create_organization

URL = "/api/v0/organization/settings"
TRANSFER = "/api/v0/organization/transfer-ownership"
SHARED_MEMBER = "member@example.test"


@contextmanager
def scope(owner):
    """Act as the tenant-local principal attached to a provisioned owner."""
    org, workspace, _, principal, _ = owner
    token = set_request_identity(principal.id, org.id, workspace.id)
    try:
        yield
    finally:
        reset_request_principal(token)


def prepare(runtime):
    """Create two owners and one shared-account member with independent roles."""
    with system_scope("bootstrap"):
        a = create_organization(
            runtime, name="Org A", email="owner-a@example.test", display_name="Owner A"
        )
        b = create_organization(
            runtime, name="Org B", email="owner-b@example.test", display_name="Owner B"
        )
    people = []
    for owner in (a, b):
        with scope(owner):
            role = next(
                r
                for r in runtime.roles.list_scoped(owner[0].id)
                if r.system_role_key == "builder"
            )
            people.append(
                runtime.user_administration_service.create_workspace_user(
                    email=SHARED_MEMBER,
                    display_name="Shared member",
                    role_id=role.id,
                )
            )
    with system_scope("account"):
        _, owner_token = sign_in(
            runtime.account_sign_in_service, "owner-a@example.test"
        )
        _, b_token = sign_in(runtime.account_sign_in_service, "owner-b@example.test")
        account, member_token = sign_in(runtime.account_sign_in_service, SHARED_MEMBER)
    return {
        "a": a,
        "b": b,
        "member_a": people[0],
        "member_b": people[1],
        "owner_token": owner_token,
        "b_token": b_token,
        "member_token": member_token,
        "member_account": account,
    }


def headers(world, token="owner_token", tenant="a"):
    """Select one organization on a bearer-authenticated account request."""
    return {
        "Authorization": "Bearer " + world[token],
        "X-Organization-Id": world[tenant][0].id,
    }


def transfer_body(world, **changes):
    """Confirm a transfer of Org A to the linked shared member."""
    return {
        "principal_id": world["member_a"][1].id,
        "expected_revision": 1,
        "confirmation": "Org A",
        **changes,
    }
