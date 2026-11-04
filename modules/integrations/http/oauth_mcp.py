"""OAuth authorization for hosted and customer-managed MCP servers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from clients.integrations.mcp import is_query_tool
from modules.integrations.catalog import CURATED_MCP_ENDPOINTS
from modules.integrations.http.oauth_context import OAuthContextBase
from shared.errors import PluginGatewayError


class McpOAuthStart(BaseModel):
    """Validated request to authorize an MCP server connection."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    endpoint_url: str = Field(min_length=12, max_length=2048)
    display_name: str = Field(min_length=2, max_length=160)
    connector_id: str = Field(default="custom-mcp", min_length=2, max_length=128)


class McpOAuthMixin(OAuthContextBase):
    """Run MCP OAuth discovery, dynamic registration, PKCE, and callback."""

    def start_mcp_oauth(self, request: McpOAuthStart) -> dict[str, str]:
        """Discover a trusted authorization server and start PKCE consent."""
        self.dependencies.state_service.authorize_current_principal()
        endpoint = self.dependencies.connection_service.mcp_plugin.validate_endpoint(
            request.endpoint_url
        )
        if (
            request.connector_id != "custom-mcp"
            and endpoint != CURATED_MCP_ENDPOINTS.get(request.connector_id)
        ):
            raise HTTPException(
                status_code=422, detail="Connector ID does not match its MCP endpoint"
            )
        if request.connector_id not in {"custom-mcp", *CURATED_MCP_ENDPOINTS}:
            raise HTTPException(status_code=422, detail="MCP connector ID is invalid")
        metadata = self._discover_oauth_metadata(endpoint)
        registration = self._register_client(metadata)
        verifier = secrets.token_urlsafe(48)
        challenge = _pkce_challenge(verifier)
        redirect_uri = self.callback_url("mcp/callback")
        temporary = {
            **registration,
            "code_verifier": verifier,
            "token_endpoint": metadata["token_endpoint"],
            "issuer": metadata["issuer"],
            "resource": endpoint,
            "redirect_uri": redirect_uri,
            "authorization_endpoint": metadata["authorization_endpoint"],
        }
        secret_ref = self.dependencies.secret_store.put(
            json.dumps(temporary, separators=(",", ":"))
        )
        try:
            state = self.state_record(
                "mcp",
                {
                    "endpoint": endpoint,
                    "display_name": request.display_name,
                    "catalog_connector_id": request.connector_id,
                },
                secret_ref=secret_ref,
            )
        except Exception:
            self.dependencies.secret_store.delete(secret_ref)
            raise
        params = {
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": redirect_uri,
            "state": state.oauth_state_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": endpoint,
        }
        scopes = metadata.get("scopes_supported")
        if isinstance(scopes, list) and scopes:
            params["scope"] = " ".join(
                scope for scope in scopes if isinstance(scope, str)
            )
        separator = "&" if "?" in metadata["authorization_endpoint"] else "?"
        return {
            "authorization_url": metadata["authorization_endpoint"]
            + separator
            + urlencode(params)
        }

    def mcp_oauth_callback(
        self,
        state: str,
        code: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Exchange the authorization code and return the user to setup."""
        token_ref: str | None = None
        keep_token_ref = False
        try:
            record = self.take_state(state, "mcp")
            self.request_as(record)
        except HTTPException:
            return self.finish("mcp", "error", "state_invalid")
        try:
            if error:
                return self.finish("mcp", "error", "authorization_denied")
            if not code or not record.secret_ref:
                raise ValueError("MCP authorization response is incomplete")
            temporary = _json_secret(
                self.dependencies.secret_store.get(record.secret_ref)
            )
            token_data = self._exchange_code(code, temporary)
            access_token = token_data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("MCP authorization did not return an access token")
            bundle = self._token_bundle(token_data, temporary)
            token_ref = self.dependencies.secret_store.put(
                json.dumps(bundle, separators=(",", ":"))
            )
            endpoint = _state_text(record.configuration, "endpoint")
            tools = self.dependencies.connection_service.mcp_plugin.discover_tools(
                endpoint, access_token, initialize=True
            )
            supported = [tool for tool in tools if is_query_tool(tool)][:50]
            if not supported:
                raise ValueError("MCP server has no supported read-only search tools")
            setup = self.state_record(
                "mcp",
                {
                    **record.configuration,
                    "resources": [
                        {
                            "name": tool["name"],
                            "title": str(tool.get("title") or tool["name"])[:160],
                            "description": str(tool.get("description") or "")[:1000],
                            "read_tool_supported": True,
                        }
                        for tool in supported
                    ],
                },
                step="resource_selection",
                secret_ref=token_ref,
            )
            keep_token_ref = True
            return self.setup_redirect("mcp", setup.oauth_state_id)
        except (KeyError, ValueError, httpx.HTTPError, TypeError, PluginGatewayError):
            return self.finish("mcp", "error", "authorization_failed")
        finally:
            if record.secret_ref:
                self.dependencies.secret_store.delete(record.secret_ref)
            if token_ref and not keep_token_ref:
                self.dependencies.secret_store.delete(token_ref)

    def _discover_oauth_metadata(self, endpoint: str) -> dict[str, Any]:
        """Discover resource and authorization metadata on allowed HTTPS hosts."""
        origin = _origin(endpoint)
        resource_path = urlsplit(endpoint).path
        resource_paths = (
            "/.well-known/oauth-protected-resource",
            f"/.well-known/oauth-protected-resource{resource_path}",
            f"{resource_path.rstrip('/')}/.well-known/oauth-protected-resource",
        )
        resource = self._first_metadata(origin, resource_paths)
        servers = resource.get("authorization_servers")
        if (
            not isinstance(servers, list)
            or not servers
            or not isinstance(servers[0], str)
        ):
            raise HTTPException(status_code=422, detail="MCP OAuth metadata is invalid")
        issuer = _safe_https_url(servers[0])
        self._assert_oauth_host_allowed(issuer)
        issuer_origin = _origin(issuer)
        issuer_path = urlsplit(issuer).path.rstrip("/")
        metadata_paths = ["/.well-known/oauth-authorization-server"]
        if issuer_path:
            metadata_paths.insert(
                0, f"/.well-known/oauth-authorization-server{issuer_path}"
            )
        metadata = self._first_metadata(issuer_origin, metadata_paths)
        if metadata.get("issuer") != issuer:
            raise HTTPException(status_code=422, detail="OAuth issuer did not match")
        for name in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
        ):
            value = metadata.get(name)
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail="MCP OAuth is unsupported")
            safe_value = _safe_https_url(value)
            self._assert_oauth_host_allowed(safe_value)
            metadata[name] = safe_value
        methods = metadata.get("code_challenge_methods_supported", ["S256"])
        if not isinstance(methods, list) or "S256" not in methods:
            raise HTTPException(
                status_code=422, detail="MCP OAuth server must support PKCE S256"
            )
        return metadata

    def _first_metadata(
        self, origin: str, paths: tuple[str, ...] | list[str]
    ) -> dict[str, Any]:
        """Fetch the first successful bounded JSON metadata document."""
        for path in paths:
            try:
                response = self.client.get(
                    origin + path, timeout=10, follow_redirects=False
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                if len(response.content) > 128_000:
                    raise ValueError("OAuth metadata exceeded its size limit")
                value = response.json()
                if isinstance(value, dict):
                    return value
            except (httpx.HTTPError, ValueError):
                continue
        raise HTTPException(status_code=422, detail="MCP OAuth metadata was not found")

    def _register_client(self, metadata: dict[str, Any]) -> dict[str, str]:
        """Register Weaves as a public MCP OAuth client using PKCE."""
        try:
            response = self.client.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": "Weaves",
                    "client_uri": self.frontend,
                    "redirect_uris": [self.callback_url("mcp/callback")],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                headers={"Accept": "application/json"},
                timeout=15,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail="MCP client registration failed"
            ) from exc
        client_id = payload.get("client_id") if isinstance(payload, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise HTTPException(
                status_code=502, detail="MCP client registration was invalid"
            )
        credentials = {"client_id": client_id}
        client_secret = payload.get("client_secret")
        if isinstance(client_secret, str) and client_secret:
            credentials["client_secret"] = client_secret
        return credentials

    def _exchange_code(self, code: str, temporary: dict[str, Any]) -> dict[str, Any]:
        """Exchange a code using the stored verifier and registered client."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": str(temporary["client_id"]),
            "redirect_uri": str(temporary["redirect_uri"]),
            "code_verifier": str(temporary["code_verifier"]),
            "resource": str(temporary["resource"]),
        }
        if temporary.get("client_secret"):
            data["client_secret"] = str(temporary["client_secret"])
        try:
            response = self.client.post(
                str(temporary["token_endpoint"]),
                data=data,
                headers={"Accept": "application/json"},
                timeout=15,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError("MCP token exchange failed") from exc
        if not isinstance(payload, dict):
            raise ValueError("MCP token response was invalid")
        return payload

    @staticmethod
    def _token_bundle(
        token_data: dict[str, Any], temporary: dict[str, Any]
    ) -> dict[str, Any]:
        """Build an encrypted token bundle understood by the runtime resolver."""
        expires_in = token_data.get("expires_in")
        seconds = int(expires_in) if isinstance(expires_in, (int, float)) else 3600
        bundle: dict[str, Any] = {
            "weaves_oauth": True,
            "provider": "mcp",
            "access_token": token_data["access_token"],
            "client_id": temporary["client_id"],
            "token_url": temporary["token_endpoint"],
            "resource": temporary["resource"],
            "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
        }
        for key in ("refresh_token", "client_secret"):
            value = token_data.get(key) or temporary.get(key)
            if isinstance(value, str) and value:
                bundle[key] = value
        return bundle

    def _assert_oauth_host_allowed(self, value: str) -> None:
        """Require OAuth endpoints to use exact deployment-approved hosts."""
        host = urlsplit(value).hostname or ""
        allowed = {
            item.strip().casefold()
            for item in os.environ.get("WEAVES_MCP_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if host.casefold() not in allowed:
            raise HTTPException(status_code=422, detail="MCP OAuth host is not allowed")


def _safe_https_url(value: str) -> str:
    """Reject insecure or credential-bearing OAuth metadata URLs."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MCP OAuth URL is unsafe") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise HTTPException(status_code=422, detail="MCP OAuth URL is unsafe")
    return value.rstrip("/")


def _origin(value: str) -> str:
    """Return the validated HTTPS origin for a metadata URL."""
    parsed = urlsplit(_safe_https_url(value))
    return f"https://{parsed.netloc}"


def _pkce_challenge(verifier: str) -> str:
    """Return the S256 PKCE challenge for a securely random verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _json_secret(value: str) -> dict[str, Any]:
    """Decode a temporary OAuth secret bundle and validate its shape."""
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP OAuth state is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("MCP OAuth state is invalid")
    return payload


def _state_text(configuration: dict[str, Any], field: str) -> str:
    """Read a required string value from validated OAuth state."""
    value = configuration.get(field)
    if not isinstance(value, str):
        raise ValueError("MCP OAuth state is invalid")
    return value


__all__ = ["McpOAuthMixin", "McpOAuthStart"]
