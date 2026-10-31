"""Shared OAuth route state, provider exchange, and redirect helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from application.ports.secrets import SecretStore
from clients.integrations.github_app_auth import GitHubAppTokenProvider
from contracts.v1 import OAuthAuthorizationState
from modules.integrations.connection_service import IntegrationConnectionService
from modules.integrations.oauth_state_service import OAuthStateService


def _now() -> datetime:
    """Return the current UTC time for OAuth state validation."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class OAuthRouteDependencies:
    """Process-scoped resources and use cases required by OAuth handlers."""

    http_client: httpx.Client
    state_service: OAuthStateService
    secret_store: SecretStore
    github_app_token_provider: GitHubAppTokenProvider
    connection_service: IntegrationConnectionService


def _state_string(record: OAuthAuthorizationState, key: str) -> str:
    value = record.configuration.get(key)
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400, detail="OAuth state configuration is invalid"
        )
    return value


def _state_strings(record: OAuthAuthorizationState, key: str) -> tuple[str, ...]:
    value = record.configuration.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(
            status_code=400, detail="OAuth state configuration is invalid"
        )
    return tuple(cast(list[str], value))


def _state_bool(record: OAuthAuthorizationState, key: str) -> bool:
    value = record.configuration.get(key)
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=400, detail="OAuth state configuration is invalid"
        )
    return value


class OAuthContextBase:
    """Hold request-independent dependencies shared by OAuth route groups."""

    def __init__(self, dependencies: OAuthRouteDependencies) -> None:
        """Prepare shared HTTP and public URL dependencies for callbacks."""
        self.dependencies = dependencies
        self.client = dependencies.http_client
        self.api_base = (
            os.environ.get("WEAVES_PUBLIC_API_BASE_URL", "").strip().rstrip("/")
        )
        self.frontend = (
            os.environ.get("WEAVES_PUBLIC_FRONTEND_URL", "https://app.weave-studio.in")
            .strip()
            .rstrip("/")
        )

    def callback_url(self, path: str) -> str:
        """Handle the callback url step of the OAuth flow."""
        if not self.api_base:
            raise HTTPException(
                status_code=503, detail="Connector OAuth is not configured"
            )

        return f"{self.api_base}/api/v0/oauth/{path}"

    def state_record(
        self,
        provider: Literal["github", "slack", "jira"],
        configuration: dict[str, Any],
        *,
        step: Literal["authorize", "github_install"] = "authorize",
        secret_ref: str | None = None,
    ) -> OAuthAuthorizationState:
        """Handle the state record step of the OAuth flow."""
        return self.dependencies.state_service.create_state(
            provider, configuration, step=step, secret_ref=secret_ref
        )

    def take_state(
        self,
        value: str,
        provider: Literal["github", "slack", "jira"],
        step: Literal["authorize", "github_install"] = "authorize",
    ) -> OAuthAuthorizationState:
        """Handle the take state step of the OAuth flow."""
        try:
            record = self.dependencies.state_service.consume_state(
                value, provider, step
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return record

    def discard_state(
        self, value: str, provider: Literal["github", "slack", "jira"]
    ) -> None:
        """Handle the discard state step of the OAuth flow."""
        try:
            self.take_state(value, provider)

        except HTTPException:
            return

    def request_as(self, record: OAuthAuthorizationState) -> None:
        """Handle the request as step of the OAuth flow."""
        self.dependencies.state_service.authorize_state_principal(record)

    def finish(self, provider: str, status: str) -> RedirectResponse:
        """Handle the finish step of the OAuth flow."""
        return RedirectResponse(
            f"{self.frontend}/?connector_oauth={status}&provider={provider}#connections",
            status_code=303,
        )

    def exchange(
        self,
        url: str,
        *,
        data: dict[str, str],
        auth: tuple[str, str] | None = None,
        json_payload: bool = False,
    ) -> dict[str, Any]:
        """Handle the exchange step of the OAuth flow."""
        try:
            request_data: dict[str, Any] = {
                "headers": {"Accept": "application/json"},
                "timeout": 20,
            }

            if json_payload:
                request_data["json"] = data

            else:
                request_data["data"] = data

            if auth is not None:
                request_data["auth"] = auth

            response = self.client.post(url, **request_data)

            response.raise_for_status()

            payload = response.json()

        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail="OAuth provider token exchange failed"
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=502, detail="OAuth provider returned an invalid response"
            )

        return payload

    def token_bundle(
        self, provider: str, payload: dict[str, Any], *, access_token: str | None = None
    ) -> str:
        """Handle the token bundle step of the OAuth flow."""
        token = access_token or payload.get("access_token")

        if not isinstance(token, str) or not token:
            raise HTTPException(
                status_code=502, detail="OAuth provider did not return an access token"
            )

        expires_in = payload.get("expires_in", 3600)

        if not isinstance(expires_in, (int, float)):
            expires_in = 3600

        return json.dumps(
            {
                "weaves_oauth": True,
                "provider": provider,
                "access_token": token,
                "refresh_token": payload.get("refresh_token"),
                "expires_at": (_now() + timedelta(seconds=int(expires_in))).isoformat(),
            },
            separators=(",", ":"),
        )
