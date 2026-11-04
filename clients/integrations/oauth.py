"""Encrypted OAuth token bundles with provider-specific refresh handling."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx


class _SecretStore(Protocol):
    def get(self, secret_ref: str) -> str: ...

    def replace(self, secret_ref: str, secret: str) -> None: ...


class OAuthTokenResolver:
    """Resolve legacy bearer tokens and refresh OAuth tokens when required."""

    def __init__(self, store: _SecretStore, client: httpx.Client) -> None:
        self._store = store
        self._client = client

    def resolve(self, secret_ref: str) -> str:
        return self._store.get(secret_ref)

    def resolve_for_plugin(self, secret_ref: str, provider: str) -> str:
        raw = self._store.get(secret_ref)
        try:
            bundle = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
        if not isinstance(bundle, dict) or bundle.get("weaves_oauth") is not True:
            return raw
        access_token = bundle.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("OAuth credential bundle has no access token")
        expiry = bundle.get("expires_at")
        if not isinstance(expiry, str):
            return access_token
        try:
            expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("OAuth credential expiry is invalid") from exc
        if expires_at.tzinfo is None or expires_at > datetime.now(UTC) + timedelta(
            seconds=90
        ):
            return access_token
        refreshed = self._refresh(provider, bundle)
        self._store.replace(secret_ref, json.dumps(refreshed, separators=(",", ":")))
        next_access = refreshed.get("access_token")
        if not isinstance(next_access, str) or not next_access:
            raise ValueError("OAuth refresh did not return an access token")
        return next_access

    def _refresh(self, provider: str, bundle: dict[str, Any]) -> dict[str, Any]:
        refresh_token = bundle.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("OAuth credential expired; reconnect this integration")
        if provider == "mcp":
            response = self._mcp_refresh_request(bundle, refresh_token)
        elif provider == "jira":
            client_id = os.environ.get("WEAVES_ATLASSIAN_OAUTH_CLIENT_ID", "").strip()
            client_secret = os.environ.get(
                "WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET", ""
            ).strip()
            url = "https://auth.atlassian.com/oauth/token"
            payload = {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            }
            response = self._client.post(
                url,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=15,
            )
        elif provider == "slack":
            client_id = os.environ.get("WEAVES_SLACK_OAUTH_CLIENT_ID", "").strip()
            client_secret = os.environ.get(
                "WEAVES_SLACK_OAUTH_CLIENT_SECRET", ""
            ).strip()
            response = self._client.post(
                "https://slack.com/api/oauth.v2.access",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(client_id, client_secret),
                timeout=15,
            )
        else:
            raise ValueError("OAuth credential expired; reconnect this integration")
        if response.is_error:
            raise ValueError(f"OAuth refresh failed with HTTP {response.status_code}")
        try:
            token_response: Any = response.json()
        except ValueError as exc:
            raise ValueError(
                "OAuth provider returned an invalid refresh response"
            ) from exc
        if not isinstance(token_response, dict) or token_response.get("ok") is False:
            raise ValueError("OAuth provider rejected token refresh")
        next_access_token = token_response.get("access_token")
        next_refresh_token = token_response.get("refresh_token") or refresh_token
        raw_expires_in = token_response.get("expires_in")
        if not isinstance(next_access_token, str) or not next_access_token:
            raise ValueError("OAuth refresh did not return an access token")
        expires_in = (
            int(raw_expires_in) if isinstance(raw_expires_in, (int, float)) else 3600
        )
        return {
            **bundle,
            "weaves_oauth": True,
            "provider": provider,
            "access_token": next_access_token,
            "refresh_token": next_refresh_token,
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=expires_in)
            ).isoformat(),
        }

    def _mcp_refresh_request(
        self, bundle: dict[str, Any], refresh_token: str
    ) -> httpx.Response:
        """Refresh an MCP OAuth token only through its approved token host."""
        from urllib.parse import urlsplit

        url = bundle.get("token_url")
        client_id = bundle.get("client_id")
        if (
            not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(client_id, str)
        ):
            raise ValueError("MCP OAuth refresh configuration is invalid")
        allowed_hosts = {
            item.strip().casefold()
            for item in os.environ.get("WEAVES_MCP_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if (urlsplit(url).hostname or "").casefold() not in allowed_hosts:
            raise ValueError("MCP OAuth refresh host is not allowed")
        refresh_payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": str(bundle.get("resource", "")),
        }
        client_secret = bundle.get("client_secret")
        if isinstance(client_secret, str) and client_secret:
            refresh_payload["client_secret"] = client_secret
        return self._client.post(
            url,
            data=refresh_payload,
            headers={"Accept": "application/json"},
            timeout=15,
            follow_redirects=False,
        )
