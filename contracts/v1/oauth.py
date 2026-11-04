"""Short-lived OAuth transactions used by browser-based connector setup."""

from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from contracts.v1.base import FrozenProductContract, OpaqueId
from contracts.v1.json_data import reject_credential_fields


class OAuthAuthorizationState(FrozenProductContract):
    """Server-side, one-time state for an OAuth authorization redirect."""

    oauth_state_id: OpaqueId
    provider: Literal["github", "slack", "jira", "mcp"]
    step: Literal["authorize", "github_install", "resource_selection"] = "authorize"
    principal_id: OpaqueId
    org_id: OpaqueId
    workspace_id: OpaqueId
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    secret_ref: str | None = None
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("configuration")
    @classmethod
    def configuration_contains_no_credentials(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        reject_credential_fields(value, "OAuth configuration")
        return value


__all__ = ["OAuthAuthorizationState"]
