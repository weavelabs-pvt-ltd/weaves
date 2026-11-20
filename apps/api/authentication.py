"""Bearer-token authentication for the product HTTP API."""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from modules.auth.account_sign_in import AccountSignInService
from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.http.browser_sessions import (
    COOKIE_NAME,
    CSRF_HEADER,
    browser_origin,
    browser_sessions_configured,
    csrf_valid,
)
from modules.auth.organization_service import OrganizationService
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)

_PUBLIC_EXACT_PATHS = {
    "/api/v0/health",
    "/api/v0/onboarding/organizations",
    "/api/v0/auth/login",
    "/api/v0/auth/email-code",
    "/api/v0/auth/email-verify",
    "/api/v0/account/email-code",
    "/api/v0/account/email-verify",
    "/api/v0/auth/sso/providers",
    "/api/v0/auth/sso/google/start",
    "/api/v0/auth/sso/google/callback",
    "/api/v0/auth/sso/microsoft/start",
    "/api/v0/auth/sso/microsoft/callback",
    "/api/v0/auth/sso/zoho/start",
    "/api/v0/auth/sso/zoho/callback",
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
    accounts: AccountSignInService
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


def _cookie_principal(
    request: Request, services: AuthenticationServices, token: str
) -> str | JSONResponse:
    """Validate same-origin cookie traffic before resolving its principal."""
    expected_origin = browser_origin()
    if (
        not browser_sessions_configured()
        or request.headers.get("host", "") != urlsplit(expected_origin).netloc
        or request.url.scheme != urlsplit(expected_origin).scheme
    ):
        return JSONResponse(
            content={"detail": "Browser session unavailable"}, status_code=401
        )
    if request.headers.get(
        "origin", expected_origin
    ) != expected_origin or request.headers.get(
        "sec-fetch-site", "same-origin"
    ) not in {"same-origin", "none"}:
        return JSONResponse(
            content={"detail": "Cross-origin request rejected"}, status_code=403
        )
    if request.method not in {"GET", "HEAD"} and (
        request.headers.get("origin") != expected_origin
        or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
        or not csrf_valid(token, request.headers.get(CSRF_HEADER, ""))
    ):
        return JSONResponse(content={"detail": "CSRF check failed"}, status_code=403)
    principal_id = services.authentication.authenticate_user_session(
        token, channel="browser"
    )
    if not principal_id:
        return JSONResponse(
            content={"detail": "Browser session invalid"}, status_code=401
        )
    return principal_id


def _bearer_principal(
    authorization: str, services: AuthenticationServices, api_tokens: dict[str, str]
) -> str | JSONResponse:
    """Preserve the existing static-token, API-token, and bearer-session path."""
    scheme, separator, supplied_token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return JSONResponse(
            content={"detail": "Invalid or missing bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    digest = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
    mapped_principal = next(
        (
            candidate
            for configured_digest, candidate in api_tokens.items()
            if hmac.compare_digest(digest, configured_digest)
        ),
        None,
    )
    try:
        if mapped_principal is not None:
            return services.credentials.authenticate_principal(mapped_principal)
        principal_id = services.authentication.authenticate_api_token(
            supplied_token
        ) or services.authentication.authenticate_user_session(supplied_token)
        if not principal_id:
            raise PermissionError("unknown or revoked token")
        return principal_id
    except (KeyError, PermissionError):
        return JSONResponse(
            content={"detail": "Invalid or disabled bearer token subject"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _account_request(
    request: Request, call_next: Any, services: AuthenticationServices
) -> Any:
    """Authenticate account routes with account bearer sessions only.

    No tenant identity is bound, and cookies, tenant sessions, API tokens and
    static tokens never reach this path.
    """
    scheme, separator, token = request.headers.get("authorization", "").partition(" ")
    unauthorized = JSONResponse(
        content={"detail": "Invalid or missing account session"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not separator or scheme.casefold() != "bearer":
        return unauthorized
    try:
        with system_scope("account"):
            request.state.account = services.accounts.sessions.authenticate(token)
    except PermissionError:
        return unauthorized
    return await call_next(request)


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
        if request.url.path.startswith("/api/v0/account/"):
            return await _account_request(request, call_next, services)
        authorization = request.headers.get("authorization", "")
        cookie_token = request.cookies.get(COOKIE_NAME, "")
        browser_only = request.url.path.startswith("/api/v0/auth/browser/")
        using_cookie = bool(cookie_token) and not authorization.strip()
        if browser_only and not using_cookie:
            return JSONResponse(
                content={"detail": "Browser session required"}, status_code=401
            )
        if using_cookie:
            with system_scope("auth"):
                principal = _cookie_principal(request, services, cookie_token)
        elif services.bearer_required and not authorization.strip():
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            with system_scope("auth"):
                principal = (
                    _bearer_principal(authorization, services, api_tokens)
                    if api_tokens or authorization
                    else "local-developer"
                )
        if isinstance(principal, JSONResponse):
            return principal
        principal_id = principal
        try:
            with system_scope("auth"):
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
            response = await call_next(request)
            if using_cookie:
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            reset_request_principal(identity_token)
