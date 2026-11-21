"""Legacy agent execution and authentication runtime delegates."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING

from contracts.v1 import (
    AgentSkillSnapshot,
    ApiCredential,
    Permission,
    Principal,
    Role,
)
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
        agent_version_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
        _runtime_skill_snapshots: tuple[AgentSkillSnapshot, ...] = (),
    ) -> RuntimeResult:
        """Delegate agent execution while preserving runtime callers."""
        return self.agent_execution_service.run_agent(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            idempotency_key,
            workflow_id=workflow_id,
            agent_version_id=agent_version_id,
            _cancellation_event=_cancellation_event,
            _cancellation_check=_cancellation_check,
            _runtime_skill_snapshots=_runtime_skill_snapshots,
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
