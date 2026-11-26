"""Install, status and disconnect routes for the Weaves Slack bot.

The start, status and disconnect routes need a signed-in organization admin. The
callback is the only unauthenticated one: it is reached from Slack and trusts
nothing but a single-use state row bound to the organization and principal that
started the install.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from modules.slack.installation_callback import complete_install
from modules.slack.installation_service import (
    InstallationNotFound,
    SlackInstallationService,
)
from modules.slack.installation_service import SlackUnavailable as _Unavailable

START_PATH = "/api/v0/oauth/slack/install"
CALLBACK_PATH = "/api/v0/oauth/slack/install/callback"
STATUS_PATH = "/api/v0/organization/slack"


def register_install_routes(app: FastAPI, service: SlackInstallationService) -> None:
    """Register the four install routes."""

    def start() -> dict[str, str]:
        try:
            return service.start()
        except _Unavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    def callback(
        state: str = Query(..., max_length=256),
        code: str | None = Query(None, max_length=512),
        error: str | None = Query(None, max_length=128),
    ) -> RedirectResponse:
        return RedirectResponse(complete_install(service, code, state, error), 303)

    def status() -> dict[str, Any]:
        return service.status()

    def disconnect(installation_id: str) -> dict[str, Any]:
        try:
            return service.disconnect(installation_id)
        except InstallationNotFound:
            raise HTTPException(
                status_code=404, detail="Slack installation not found"
            ) from None

    app.get(START_PATH)(start)
    app.get(CALLBACK_PATH, include_in_schema=False)(callback)
    app.get(STATUS_PATH)(status)
    app.delete(f"{STATUS_PATH}/{{installation_id}}")(disconnect)
