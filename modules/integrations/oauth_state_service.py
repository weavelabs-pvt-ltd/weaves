"""One-time OAuth state creation and validation use cases."""

from __future__ import annotations

import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from contracts.v1 import OAuthAuthorizationState, Permission
from modules.auth.credential_service import CredentialService
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
    set_request_identity,
)


class OAuthStateService:
    """Manage short-lived, single-use OAuth state records."""

    def __init__(
        self,
        *,
        states: Repository[OAuthAuthorizationState],
        credential_service: CredentialService,
        secret_store: SecretStore | None = None,
    ) -> None:
        """Bind state storage to the platform identity use cases."""
        self._states = states
        self._credential_service = credential_service
        self._secret_store = secret_store

    def authorize_current_principal(self) -> str:
        """Authorize the current developer principal to manage connectors."""
        principal_id = self._credential_service.authenticate_principal(
            "local-developer"
        )
        self._credential_service.authorize(principal_id, Permission.PLUGINS_MANAGE)
        return principal_id

    def create_state(
        self,
        provider: Literal["github", "slack", "jira"],
        configuration: dict[str, Any],
        *,
        step: Literal[
            "authorize", "github_install", "resource_selection"
        ] = "authorize",
        secret_ref: str | None = None,
    ) -> OAuthAuthorizationState:
        """Create a ten-minute state record tied to the active workspace."""
        principal_id = effective_principal_id(self.authorize_current_principal())
        now = datetime.now(UTC)
        self._remove_expired(now)
        record = OAuthAuthorizationState(
            oauth_state_id=secrets.token_urlsafe(32),
            provider=provider,
            step=step,
            principal_id=principal_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            configuration=configuration,
            secret_ref=secret_ref,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        self._states.create(record)
        return record

    def consume_state(
        self,
        value: str,
        provider: Literal["github", "slack", "jira"],
        step: Literal[
            "authorize", "github_install", "resource_selection"
        ] = "authorize",
    ) -> OAuthAuthorizationState:
        """Consume and validate one state value, rejecting replay and expiry."""
        try:
            record = self._states.consume(value)
        except KeyError as exc:
            raise ValueError("Authorization state is invalid or already used") from exc
        if (
            record.provider != provider
            or record.step != step
            or record.expires_at <= datetime.now(UTC)
        ):
            if record.secret_ref and self._secret_store:
                self._secret_store.delete(record.secret_ref)
            raise ValueError("Authorization state is invalid or expired")
        return record

    def authorize_state_principal(self, record: OAuthAuthorizationState) -> None:
        """Restore the state owner's workspace and recheck connector permission."""
        set_request_identity(record.principal_id, record.org_id, record.workspace_id)
        self._credential_service.authorize(
            record.principal_id, Permission.PLUGINS_MANAGE
        )

    def get_resource_selection(self, state_id: str) -> OAuthAuthorizationState:
        """Load one pending provider setup owned by the signed-in workspace."""
        principal_id = effective_principal_id(self.authorize_current_principal())
        record = self._states.get(state_id)
        if (
            record.principal_id == principal_id
            and record.org_id == effective_org_id()
            and record.workspace_id == effective_workspace_id()
            and record.expires_at <= datetime.now(UTC)
        ):
            with suppress(KeyError):
                self._states.delete(state_id)
            if record.secret_ref and self._secret_store:
                self._secret_store.delete(record.secret_ref)
            raise ValueError("Connector setup expired; start the connection again")
        if (
            record.step != "resource_selection"
            or record.principal_id != principal_id
            or record.org_id != effective_org_id()
            or record.workspace_id != effective_workspace_id()
        ):
            raise ValueError("Connector setup expired or belongs to another workspace")
        return record

    def _remove_expired(self, now: datetime) -> None:
        """Remove expired state records before creating a fresh authorization."""
        for expired in self._states.list():
            if expired.expires_at <= now:
                with suppress(KeyError):
                    self._states.delete(expired.oauth_state_id)
                if expired.secret_ref and self._secret_store:
                    self._secret_store.delete(expired.secret_ref)
