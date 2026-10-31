"""Organization identity, access, and credential API routes."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.http.credential_routes import register_credential_routes
from modules.auth.http.session_routes import register_session_routes
from modules.auth.http.user_routes import register_user_routes
from modules.auth.http.workspace_routes import register_workspace_routes
from modules.auth.invitation_service import InvitationService
from modules.auth.organization_service import OrganizationService
from modules.auth.user_administration_service import UserAdministrationService
from modules.auth.workspace_access_service import WorkspaceAccessService


def register_identity_routes(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    authentication_service: AuthenticationService,
    credential_service: CredentialService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register organization, workspace, user, session, and token endpoints."""
    register_workspace_routes(app, organization_service, workspace_access_service, dump)
    register_user_routes(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    register_session_routes(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
    register_credential_routes(app, credential_service, dump)
