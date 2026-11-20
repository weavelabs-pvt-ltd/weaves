"""Bearer-token authentication for the product HTTP API."""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from modules.auth.account_sessions import ACCOUNT_TOKEN_PREFIX
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.http.browser_sessions import (
    ACCOUNT_COOKIE_NAME,
    COOKIE_NAME,
    request_refusal,
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
    "/api/v0/account/browser/email-verify",
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
    refusal = request_refusal(request, token)
    if refusal is not None:
        return refusal
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


async def _account_browser_request(
    request: Request, call_next: Any, services: AuthenticationServices
) -> Any:
    """Authenticate /account/browser/* with the account cookie only.

    The cookie must be the sole credential: any Authorization header, a missing
    cookie, a bearer-channel token or a legacy tenant cookie all fail here.
    """
    token = request.cookies.get(ACCOUNT_COOKIE_NAME, "")
    if not token or request.headers.get("authorization", "").strip():
        return JSONResponse(
            content={"detail": "Browser session required"}, status_code=401
        )
    refusal = request_refusal(request, token)
    if refusal is not None:
        return refusal
    try:
        with system_scope("account"):
            request.state.account = services.accounts.sessions.authenticate(
                token, "browser"
            )
    except PermissionError:
        return JSONResponse(
            content={"detail": "Browser session invalid"}, status_code=401
        )
    return await call_next(request)


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


def _account_token(authorization: str) -> str:
    """Return an account session token from an Authorization header, else ''."""
    scheme, _, value = authorization.partition(" ")
    if scheme.casefold() == "bearer" and value.startswith(ACCOUNT_TOKEN_PREFIX):
        return value
    return ""


async def _account_tenant_request(
    request: Request,
    call_next: Any,
    services: AuthenticationServices,
    token: str,
    channel: Literal["bearer", "browser"] = "bearer",
) -> Any:
    """Let an account session act inside one organization it currently belongs to.

    The organization comes from ``X-Organization-Id`` on every request; there is
    no mutable "current organization". Authorization is re-derived from the
    membership, user, principal and organization, and the resulting identity is
    the tenant-local principal, so roles, audit and ownership stay unchanged.
    """
    organization_id = request.headers.get("x-organization-id", "").strip()
    if not organization_id and channel == "browser":
        return JSONResponse(
            content={"detail": "Organization required"}, status_code=401
        )
    if not organization_id:
        return JSONResponse(
            content={"detail": "Invalid or missing bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        with system_scope("account"):
            account = services.accounts.sessions.authenticate(token, channel)
            membership = services.accounts.selector.select(account, organization_id)
    except PermissionError:
        denied = "Organization not available"
        return JSONResponse(content={"detail": denied}, status_code=403)
    try:
        with system_scope("auth"):
            org_id, workspace_id = services.organizations.resolve_principal_scope(
                membership.principal_id, request.headers.get("x-workspace-id")
            )
    except (KeyError, PermissionError):
        return JSONResponse(
            content={"detail": "Organization not available"}, status_code=403
        )
    identity_token = set_request_identity(membership.principal_id, org_id, workspace_id)
    try:
        response = await call_next(request)
        if channel == "browser":
            response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        reset_request_principal(identity_token)


async def _account_traffic(
    request: Request,
    call_next: Any,
    services: AuthenticationServices,
    authorization: str,
) -> Any:
    """Handle account routes and account-token tenant requests; None otherwise."""
    path = request.url.path
    if path.startswith("/api/v0/account/browser/"):
        return await _account_browser_request(request, call_next, services)
    if path.startswith("/api/v0/account/"):
        return await _account_request(request, call_next, services)
    token = _account_token(authorization)
    if token:
        return await _account_tenant_request(request, call_next, services, token)
    return await _account_cookie_tenant(request, call_next, services, authorization)


async def _account_cookie_tenant(
    request: Request,
    call_next: Any,
    services: AuthenticationServices,
    authorization: str,
) -> Any:
    """Serve tenant routes from an account cookie; None when it is not in play.

    The legacy tenant cookie keeps precedence when both are present, and the
    legacy browser-only routes never accept an account cookie.
    """
    token = request.cookies.get(ACCOUNT_COOKIE_NAME, "")
    if (
        not token
        or authorization.strip()
        or request.cookies.get(COOKIE_NAME)
        or request.url.path.startswith("/api/v0/auth/browser/")
    ):
        return None
    refusal = request_refusal(request, token)
    if refusal is not None:
        return refusal
    return await _account_tenant_request(request, call_next, services, token, "browser")


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
        authorization = request.headers.get("authorization", "")
        account_response = await _account_traffic(
            request, call_next, services, authorization
        )
        if account_response is not None:
            return account_response
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
