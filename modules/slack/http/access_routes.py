"""Routes of the dashboard's "Slack access" page (permission ``slack.manage``).

Every route is organization-scoped by the request's validated tenant context and
refuses callers without ``slack.manage`` (Owner and Admin by default) with 403.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Response

from contracts.v1.slack_access import (
    SLACK_CHANNEL_ID_PATTERN,
    SLACK_USER_ID_PATTERN,
    SlackAllowedChannelRequest,
    SlackAllowedUserRequest,
    SlackPauseRequest,
    SlackSettingsUpdate,
)
from modules.slack.access.context import SlackAccessConflict
from modules.slack.access.service import SlackAccessService

BASE = "/api/v0/organization/slack/access"
UserId = Annotated[str, Path(pattern=SLACK_USER_ID_PATTERN)]
ChannelId = Annotated[str, Path(pattern=SLACK_CHANNEL_ID_PATTERN)]


def _perform[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except SlackAccessConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def register_slack_access_routes(app: FastAPI, service: SlackAccessService) -> None:
    """Expose settings, pause, allowlists and recent Execute attempts."""
    _register_settings(app, service)
    _register_allowlists(app, service)


def _register_settings(app: FastAPI, service: SlackAccessService) -> None:
    @app.get(BASE)
    def get_access(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _perform(service.view)

    @app.put(BASE)
    def put_access(payload: SlackSettingsUpdate) -> dict[str, Any]:
        return _perform(lambda: service.update(payload, payload.expected_version))

    @app.post(f"{BASE}/pause")
    def post_pause(payload: SlackPauseRequest) -> dict[str, Any]:
        return _perform(lambda: service.set_paused(payload.paused))

    @app.get(f"{BASE}/attempts")
    def get_attempts(response: Response) -> list[dict[str, Any]]:
        response.headers["Cache-Control"] = "no-store"
        return _perform(service.recent_attempts)


def _register_allowlists(app: FastAPI, service: SlackAccessService) -> None:
    @app.get(f"{BASE}/people")
    def get_people(response: Response) -> list[dict[str, Any]]:
        response.headers["Cache-Control"] = "no-store"
        return _perform(service.allowlists.list_users)

    @app.post(f"{BASE}/people")
    def post_person(payload: SlackAllowedUserRequest) -> dict[str, Any]:
        return _perform(
            lambda: service.allowlists.add_user(
                payload.slack_user_id, payload.principal_id, payload.note
            )
        )

    @app.delete(f"{BASE}/people/{{slack_user_id}}", status_code=204)
    def delete_person(slack_user_id: UserId) -> Response:
        _perform(lambda: service.allowlists.remove_user(slack_user_id))
        return Response(status_code=204)

    @app.get(f"{BASE}/channels")
    def get_channels(response: Response) -> list[dict[str, Any]]:
        response.headers["Cache-Control"] = "no-store"
        return _perform(service.allowlists.list_channels)

    @app.post(f"{BASE}/channels")
    def post_channel(payload: SlackAllowedChannelRequest) -> dict[str, Any]:
        return _perform(
            lambda: service.allowlists.add_channel(
                payload.channel_id, payload.name_cached
            )
        )

    @app.delete(f"{BASE}/channels/{{channel_id}}", status_code=204)
    def delete_channel(channel_id: ChannelId) -> Response:
        _perform(lambda: service.allowlists.remove_channel(channel_id))
        return Response(status_code=204)
