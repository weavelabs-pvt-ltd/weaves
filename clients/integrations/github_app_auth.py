"""GitHub App JWT and short-lived installation token support."""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from threading import RLock

import httpx

from shared.errors import PluginGatewayError


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class GitHubAppTokenProvider:
    """Mint and cache installation tokens limited to configured repositories."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._lock = RLock()
        self._cache: dict[tuple[str, tuple[str, ...]], tuple[str, datetime]] = {}

    @property
    def configured(self) -> bool:
        return bool(
            os.environ.get("WEAVES_GITHUB_APP_ID", "").strip()
            and os.environ.get("WEAVES_GITHUB_APP_PRIVATE_KEY", "").strip()
        )

    def installation_token(
        self, installation_id: str, repositories: tuple[str, ...] | None
    ) -> str:
        """Mint an installation token limited to the requested repositories."""
        if repositories is None or not repositories:
            raise PluginGatewayError(
                "plugin.configuration_invalid",
                "GitHub runtime tokens require an explicit repository allowlist",
            )
        return self._installation_token(installation_id, repositories)

    def installation_discovery_token(self, installation_id: str) -> str:
        """Mint a temporary token for post-install repository discovery only."""
        return self._installation_token(installation_id, None)

    def _installation_token(
        self, installation_id: str, repositories: tuple[str, ...] | None
    ) -> str:
        if not self.configured:
            raise PluginGatewayError(
                "plugin.oauth_unavailable",
                "GitHub App authentication is not configured",
            )
        selected = tuple(sorted(set(repositories or ())))
        if repositories is not None and not selected:
            raise PluginGatewayError(
                "plugin.configuration_invalid",
                "GitHub installation has no repositories",
            )
        cache_key = (installation_id, selected)
        now = datetime.now(UTC)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[1] > now + timedelta(minutes=5):
                return cached[0]
        app_jwt = self._app_jwt()
        repo_names = [repository.split("/", 1)[1] for repository in selected]
        token_request: dict[str, object] = {
            "permissions": {
                "contents": "read",
                "issues": "read",
                "pull_requests": "read",
            }
        }
        if repositories is not None:
            token_request["repositories"] = repo_names
        try:
            response = self._client.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                json=token_request,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                "plugin.transport_error", "GitHub token service request failed"
            ) from exc
        if response.is_error:
            raise PluginGatewayError(
                "plugin.oauth_token_error",
                "GitHub could not issue an installation token "
                f"(HTTP {response.status_code})",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid token response"
            ) from exc
        token = data.get("token") if isinstance(data, dict) else None
        expires = data.get("expires_at") if isinstance(data, dict) else None
        if not isinstance(token, str) or not isinstance(expires, str):
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid token response"
            )
        try:
            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid token expiry"
            ) from exc
        with self._lock:
            self._cache[cache_key] = (token, expiry)
        return token

    def _app_jwt(self) -> str:
        app_id = os.environ.get("WEAVES_GITHUB_APP_ID", "").strip()
        private_key = os.environ.get("WEAVES_GITHUB_APP_PRIVATE_KEY", "").strip()
        now = int(datetime.now(UTC).timestamp())
        header = _b64url(
            json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode()
        )
        payload = _b64url(
            json.dumps(
                {"iat": now - 60, "exp": now + 8 * 60, "iss": app_id},
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode("ascii")
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa

            key = serialization.load_pem_private_key(
                private_key.encode("utf-8"), password=None
            )
            if not isinstance(key, rsa.RSAPrivateKey):
                raise TypeError("GitHub App key must be RSA")
            signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        except Exception as exc:
            raise PluginGatewayError(
                "plugin.oauth_configuration_error", "GitHub App private key is invalid"
            ) from exc
        return f"{header}.{payload}.{_b64url(signature)}"
