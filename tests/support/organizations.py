"""Create tenant organizations for tests without any public signup route."""

from __future__ import annotations

from typing import Any


def onboard(
    client: Any, *, name: str, owner_email: str, owner_display_name: str
) -> dict[str, Any]:
    """Provision an organization with an owner API credential on a started client.

    Returns the organization, workspace, owner user, principal and the owner's
    one-time API credential as ``access_token``.
    """
    organizations = client.app.state.authentication_services.organizations
    owner = organizations.provision_organization(
        name=name,
        email=owner_email,
        display_name=owner_display_name,
        audit_metadata={"source": "test"},
    )
    assert owner.token is not None
    return {  # shape of the retired onboarding response
        "organization": owner.organization.model_dump(mode="json"),
        "workspace": owner.workspace.model_dump(mode="json"),
        "owner": owner.user.model_dump(mode="json"),
        "principal": owner.principal.model_dump(mode="json"),
        "access_token": owner.token,
    }


def create_organization(
    runtime: Any, *, name: str, email: str, display_name: str
) -> tuple[Any, Any, Any, Any, str]:
    """Provision an organization directly on a runtime.

    Returns (organization, workspace, user, principal, owner API credential).
    """
    owner = runtime.organization_service.provision_organization(
        name=name,
        email=email,
        display_name=display_name,
        audit_metadata={"source": "test"},
    )
    assert owner.token is not None
    return owner.organization, owner.workspace, owner.user, owner.principal, owner.token
