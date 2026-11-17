"""Same-origin browser session inspection and logout."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

from modules.auth.authentication_service import AuthenticationService
from modules.auth.http.browser_sessions import COOKIE_NAME, csrf_token
from shared.request_context import effective_principal_id


def register_browser_session_routes(
    app: FastAPI, authentication: AuthenticationService
) -> None:
    """Register cookie-only routes; middleware validates session and CSRF."""

    @app.get("/api/v0/auth/browser/me")
    def browser_me(request: Request, response: Response) -> dict[str, object]:
        principal, user = authentication.get_authenticated_identity(
            effective_principal_id("local-developer")
        )
        response.headers["Cache-Control"] = "no-store"
        return {
            "principal": principal.model_dump(mode="json"),
            "user": user.model_dump(mode="json") if user else None,
            "csrf_token": csrf_token(request.cookies[COOKIE_NAME]),
        }

    @app.post("/api/v0/auth/browser/logout", status_code=204)
    def browser_logout(request: Request) -> Response:
        authentication.revoke_user_session(
            request.cookies[COOKIE_NAME],
            requested_by_principal_id=effective_principal_id("local-developer"),
        )
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response
