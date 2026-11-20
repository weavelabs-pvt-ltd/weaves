"""OIDC sign-in using provider-verified identity and existing provisioned access."""

import json
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from pydantic import JsonValue
from starlette.responses import HTMLResponse, Response

from modules.auth.authentication_service import AuthenticationService
from modules.auth.organization_service import OrganizationService
from shared.request_context import system_scoped

PROVIDERS = ("google", "microsoft", "zoho")


def _metadata(provider: str) -> str | None:
    if provider == "google":
        return "https://accounts.google.com/.well-known/openid-configuration"
    if provider == "zoho":
        return "https://accounts.zoho.com/.well-known/openid-configuration"
    tenant = os.getenv("WEAVES_SSO_MICROSOFT_TENANT_ID", "")
    if re.fullmatch(r"[a-fA-F0-9-]{36}", tenant):
        return (
            f"https://login.microsoftonline.com/{tenant}/v2.0/"
            ".well-known/openid-configuration"
        )
    return None


def _verified_email(provider: str, claims: dict[str, JsonValue]) -> str:
    """Never link an account through an unverified or arbitrary email claim."""
    if provider == "microsoft":
        # A Microsoft email claim alone is not proof of mailbox ownership.
        mapping = json.loads(os.getenv("WEAVES_SSO_MICROSOFT_SUBJECTS", "{}") or "{}")
        email = mapping.get(f"{claims.get('tid')}:{claims.get('oid')}", "")
    elif claims.get("email_verified") is True:
        email = claims.get("email", "")
    else:
        raise PermissionError("Verified account identity is required")
    if not isinstance(email, str) or "@" not in email:
        raise PermissionError("Verified account identity is required")
    return email.strip().casefold()


def _complete_signin(
    auth: AuthenticationService,
    organizations: OrganizationService,
    provider: str,
    claims: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Reuse provisioned-user eligibility and normal revocable bearer sessions."""
    email = _verified_email(provider, claims)
    matches = auth._matching_email_users(email, None)
    if len(matches) != 1:
        raise PermissionError("Ask an Admin to provision workspace access")
    user = matches[0]
    principal = auth.principals.find_by_field("user_id", user.id)
    if principal is None or not auth._eligible_for_email_login(user, principal):
        raise PermissionError("Account access is disabled")
    organizations.resolve_principal_scope(principal.id)
    session, token = auth.create_user_session(principal)
    return {
        "access_token": token,
        "expires_at": session.expires_at.isoformat(),
        "user": user.model_dump(mode="json"),
    }


def _popup_response(payload: dict[str, JsonValue]) -> HTMLResponse:
    """Return credentials only to the configured frontend opener, never in URLs."""
    frontend = urlsplit(
        os.getenv("WEAVES_PUBLIC_FRONTEND_URL", "http://localhost:8080")
    )
    origin = f"{frontend.scheme}://{frontend.netloc}"
    data = json.dumps({"type": "weaves:sso", **payload}).replace("<", "\\u003c")
    nonce = uuid4().hex
    return HTMLResponse(
        "<!doctype html><title>Weaves sign-in</title>"
        "<p>Return to your Weaves sign-in window.</p>"
        f'<script nonce="{nonce}">'
        f"if(window.opener){{window.opener.postMessage({data},{json.dumps(origin)});"
        "window.close();}</script>",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'"
            ),
        },
    )


def register_sso_routes(
    app: FastAPI,
    authentication: AuthenticationService,
    organizations: OrganizationService,
) -> None:
    """Register three providers using Authlib's state, nonce and JWT validation."""
    oauth = OAuth()
    configured = set()
    secret = os.getenv("WEAVES_SSO_SESSION_SECRET", "")
    for provider in PROVIDERS:
        prefix = f"WEAVES_SSO_{provider.upper()}"
        client_id = os.getenv(prefix + "_CLIENT_ID", "")
        client_secret = os.getenv(prefix + "_CLIENT_SECRET", "")
        metadata = _metadata(provider)
        if not (client_id and client_secret and metadata and len(secret) >= 32):
            continue
        oauth.register(
            provider,
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=metadata,
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",
            },
        )
        configured.add(provider)
    app.state.signin_oauth = oauth

    @app.get("/api/v0/auth/sso/providers")
    def providers() -> list[dict[str, str | bool]]:
        return [{"provider": p, "configured": p in configured} for p in PROVIDERS]

    @app.get("/api/v0/auth/sso/{provider}/start")
    @system_scoped("auth")
    async def start(provider: str, request: Request) -> Response:
        if provider not in configured:
            return _popup_response(
                {"status": "failed", "provider": provider, "code": "SSO-NOT-CONFIGURED"}
            )
        base = os.getenv("WEAVES_PUBLIC_API_BASE_URL", "http://localhost:8001").rstrip(
            "/"
        )
        callback = f"{base}/api/v0/auth/sso/{provider}/callback"
        return await _authorization_redirect(oauth, provider, request, callback)

    @app.get("/api/v0/auth/sso/{provider}/callback")
    @system_scoped("auth")
    async def callback(provider: str, request: Request) -> HTMLResponse:
        if provider not in configured:
            raise HTTPException(404, "Sign-in provider not configured")
        try:
            client = oauth.create_client(provider)
            token = await client.authorize_access_token(request)
            claims = token.get("userinfo")
            if not claims or not claims.get("sub"):
                raise PermissionError("Provider did not verify the identity")
            result = _complete_signin(authentication, organizations, provider, claims)
            return _popup_response(
                {"status": "success", "provider": provider, **result}
            )
        except Exception:
            cancelled = request.query_params.get("error") == "access_denied"
            return _popup_response(
                {
                    "status": "cancelled" if cancelled else "failed",
                    "provider": provider,
                    "code": "SSO-" + uuid4().hex[:8].upper(),
                }
            )


async def _authorization_redirect(
    oauth: OAuth, provider: str, request: Request, callback: str
) -> Response:
    """Validate the third-party client response at the OAuth boundary."""
    client = oauth.create_client(provider)
    response = await client.authorize_redirect(request, callback)
    if not isinstance(response, Response):
        raise RuntimeError("Sign-in provider returned an invalid redirect")
    return response
