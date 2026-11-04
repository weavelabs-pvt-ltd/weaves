"""Bearer-token authentication for the product HTTP API."""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from starlette.responses import JSONResponse

from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.organization_service import OrganizationService
from shared.request_context import reset_request_principal, set_request_identity

_PUBLIC_EXACT_PATHS = {
    "/api/v0/health",
    "/api/v0/onboarding/organizations",
    "/api/v0/auth/login",
    "/api/v0/auth/accept-invitation",
    "/api/v0/oauth/github/callback",
    "/api/v0/oauth/github/installation",
    "/api/v0/oauth/slack/callback",
    "/api/v0/oauth/jira/callback",
    "/api/v0/oauth/mcp/callback",
}


@dataclass(frozen=True)
class AuthenticationServices:
    """Capability services and storage policy used by bearer middleware."""

    authentication: AuthenticationService
    credentials: CredentialService
    organizations: OrganizationService
    bearer_required: bool


def _load_api_tokens() -> dict[str, str]:
    api_tokens: dict[str, str] = {}
    raw_api_tokens = os.environ.get("WEAVES_API_TOKENS", "").strip()
    if raw_api_tokens:
        try:
            configured_tokens = json.loads(raw_api_tokens)
        except json.JSONDecodeError as exc:
            raise ValueError("WEAVES_API_TOKENS must contain a JSON array") from exc
        if not isinstance(configured_tokens, list):
            raise ValueError("WEAVES_API_TOKENS must contain a JSON array")
        for item in configured_tokens:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("token"), str)
                or not item["token"].strip()
                or not isinstance(item.get("principal_id"), str)
                or not item["principal_id"].strip()
            ):
                raise ValueError(
                    "Each WEAVES_API_TOKENS item needs token and principal_id strings"
                )
            digest = hashlib.sha256(item["token"].encode("utf-8")).hexdigest()
            if digest in api_tokens:
                raise ValueError("WEAVES_API_TOKENS contains a duplicate token")
            api_tokens[digest] = item["principal_id"].strip()
    legacy_api_token = os.environ.get("WEAVES_API_TOKEN", "").strip()
    if legacy_api_token:
        digest = hashlib.sha256(legacy_api_token.encode("utf-8")).hexdigest()
        api_tokens.setdefault(digest, "local-developer")
    return api_tokens


def install_authentication(app: FastAPI) -> None:
    """Require a bearer token on product routes and bind request identity."""
    api_tokens = _load_api_tokens()

    @app.middleware("http")
    async def require_api_token(request: Any, call_next: Any) -> Any:
        """Authenticate configured bearer tokens and propagate their principal."""
        if (
            request.method == "OPTIONS"
            or request.url.path in _PUBLIC_EXACT_PATHS
            or request.url.path.startswith("/api/v0/webhooks/")
            or not request.url.path.startswith("/api/v0/")
        ):
            return await call_next(request)
        services: AuthenticationServices = request.app.state.authentication_services
        principal_id = "local-developer"
        authorization = request.headers.get("authorization", "")
        if services.bearer_required and not authorization.strip():
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if api_tokens or authorization:
            scheme, separator, supplied_token = authorization.partition(" ")
            digest = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
            mapped_principal = next(
                (
                    candidate
                    for configured_digest, candidate in api_tokens.items()
                    if hmac.compare_digest(digest, configured_digest)
                ),
                None,
            )
            if not separator or scheme.casefold() != "bearer":
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing bearer token"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                if mapped_principal is not None:
                    principal_id = services.credentials.authenticate_principal(
                        mapped_principal
                    )
                else:
                    principal_id = (
                        services.authentication.authenticate_api_token(supplied_token)
                        or services.authentication.authenticate_user_session(
                            supplied_token
                        )
                        or ""
                    )
                if not principal_id:
                    raise PermissionError("unknown or revoked token")
            except (KeyError, PermissionError):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or disabled bearer token subject"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        try:
            org_id, workspace_id = services.organizations.resolve_principal_scope(
                principal_id, request.headers.get("x-workspace-id")
            )
        except (KeyError, PermissionError):
            return JSONResponse(
                status_code=403,
                content={"detail": "Principal cannot access the requested workspace"},
            )
        identity_token = set_request_identity(principal_id, org_id, workspace_id)
        try:
            return await call_next(request)
        finally:
            reset_request_principal(identity_token)
