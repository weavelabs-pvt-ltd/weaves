"""Validate the ``oauth.v2.access`` answer of a bot install; reasons are codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from modules.slack.install_settings import REQUIRED_SCOPES


class InstallRefused(Exception):
    """The install cannot proceed; ``reason`` is a stable code, never Slack text."""

    def __init__(self, reason: str) -> None:
        """Keep the reason as the whole message."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BotInstall:
    """The fields of a valid bot install; the token never prints."""

    access_token: str = field(repr=False)
    team_id: str
    bot_user_id: str
    team_name: str | None
    enterprise_id: str | None
    app_id: str | None
    scopes: tuple[str, ...]
    slack_user: str | None


def _text(value: Any, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def token_of(payload: dict[str, Any]) -> str | None:
    """The bot token of an answer, even one that is about to be refused."""
    return _text(payload.get("access_token"), 512)


def parse_install(payload: dict[str, Any]) -> BotInstall:
    """Return the install, or raise ``InstallRefused`` (fail closed)."""
    if payload.get("ok") is not True:
        raise InstallRefused("provider_response")
    token = token_of(payload)
    bot_user = _text(payload.get("bot_user_id"))
    if token is None or bot_user is None:
        raise InstallRefused("provider_response")
    team = _section(payload, "team")
    team_id = _text(team.get("id"))
    # Enterprise Grid org-wide installs have no single workspace to bind (decision 9).
    if payload.get("is_enterprise_install") is True or team_id is None:
        raise InstallRefused("enterprise_install_unsupported")
    raw_scopes = payload.get("scope")
    scopes = tuple(
        sorted({s.strip() for s in raw_scopes.split(",") if s.strip()})
        if isinstance(raw_scopes, str)
        else ()
    )
    if not set(scopes) >= REQUIRED_SCOPES:
        raise InstallRefused("scopes_missing")
    return BotInstall(
        access_token=token,
        team_id=team_id,
        bot_user_id=bot_user,
        team_name=_text(team.get("name")),
        enterprise_id=_text(_section(payload, "enterprise").get("id")),
        app_id=_text(payload.get("app_id")),
        scopes=scopes,
        slack_user=_text(_section(payload, "authed_user").get("id")),
    )
