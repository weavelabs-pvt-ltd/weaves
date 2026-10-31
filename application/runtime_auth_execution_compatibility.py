"""Legacy agent execution and authentication runtime delegates."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING

from contracts.v1 import ApiCredential, Permission, Principal, Role, User, UserSession
from modules.workflows.runtime_result import RuntimeResult

if TYPE_CHECKING:
    from modules.agents.execution_service import AgentExecutionService
    from modules.auth.authentication_service import AuthenticationService
    from modules.auth.credential_service import CredentialService


class RuntimeAuthExecutionCompatibilityMixin:
    """Preserve runtime auth and agent-run method names during migration."""

    agent_execution_service: AgentExecutionService
    authentication_service: AuthenticationService
    credential_service: CredentialService

    def run_agent(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Delegate agent execution while preserving runtime callers."""
        return self.agent_execution_service.run_agent(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            idempotency_key,
            workflow_id=workflow_id,
            _cancellation_event=_cancellation_event,
            _cancellation_check=_cancellation_check,
        )

    def create_service_account(
        self,
        *,
        name: str,
        permissions: tuple[Permission, ...],
        expires_in_days: int | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[Principal, Role, ApiCredential, str]:
        """Delegate service account creation to the credential service."""
        return self.credential_service.create_service_account(
            name=name,
            permissions=permissions,
            expires_in_days=expires_in_days,
            requested_by_principal_id=requested_by_principal_id,
        )

    def revoke_api_credential(
        self,
        credential_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> ApiCredential:
        """Delegate API credential revocation to the credential service."""
        return self.credential_service.revoke_api_credential(
            credential_id, requested_by_principal_id=requested_by_principal_id
        )

    def rotate_api_credential(
        self,
        credential_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[ApiCredential, str]:
        """Delegate API credential rotation to the credential service."""
        return self.credential_service.rotate_api_credential(
            credential_id, requested_by_principal_id=requested_by_principal_id
        )

    def authenticate_api_token(self, token: str) -> str | None:
        """Delegate API token validation to the authentication service."""
        return self.authentication_service.authenticate_api_token(token)

    def set_user_password(
        self,
        password: str,
        *,
        current_password: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> None:
        """Delegate password changes to the authentication service."""
        self.authentication_service.set_user_password(
            password,
            current_password=current_password,
            requested_by_principal_id=requested_by_principal_id,
        )

    def authenticate_user_password(
        self,
        email: str,
        password: str,
        *,
        org_id: str | None = None,
        client_address: str | None = None,
    ) -> tuple[User, Principal]:
        """Delegate throttled password login to the authentication service."""
        return self.authentication_service.authenticate_user_password(
            email, password, org_id=org_id, client_address=client_address
        )

    def create_user_session(self, principal: Principal) -> tuple[UserSession, str]:
        """Delegate user session creation to the authentication service."""
        return self.authentication_service.create_user_session(principal)

    def authenticate_user_session(self, token: str) -> str | None:
        """Delegate bearer session validation to the authentication service."""
        return self.authentication_service.authenticate_user_session(token)

    def revoke_user_session(
        self, token: str, *, requested_by_principal_id: str
    ) -> None:
        """Delegate session revocation to the authentication service."""
        self.authentication_service.revoke_user_session(
            token, requested_by_principal_id=requested_by_principal_id
        )
