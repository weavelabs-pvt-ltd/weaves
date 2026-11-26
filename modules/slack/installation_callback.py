"""The Slack bot install callback: exchange the code, store the token, bind the team.

Every failure is a stable reason code on the redirect back into the app; no
Slack text, token or code is ever logged or echoed. A token Slack issued for an
install we then refuse is revoked on the spot (best effort).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

from application.ports.slack import SlackInstallation
from clients.slack.errors import SlackApiError
from contracts.v1 import OAuthAuthorizationState, Permission
from modules.slack.installation_payload import (
    BotInstall,
    InstallRefused,
    parse_install,
    token_of,
)
from modules.slack.installation_service import SECRET_PURPOSE, SlackInstallationService
from shared.clock import new_id
from shared.request_context import system_scope

logger = logging.getLogger("product.slack")


def _redirect(service: SlackInstallationService, ok: bool, reason: str | None) -> str:
    query: dict[str, str] = {"slack_install": "ok" if ok else "failed"}
    if reason:
        query["slack_error"] = reason
    return f"{service.settings.frontend}/?{urlencode(query)}#connections"


def _consume(service: SlackInstallationService, state: str) -> OAuthAuthorizationState:
    with system_scope("oauth"):
        return service.state_service.consume_state(state, "slack_bot")


def complete_install(
    service: SlackInstallationService, code: str | None, state: str, error: str | None
) -> str:
    """Finish an install; returns the redirect URL (success or a reason code)."""
    try:
        record = _consume(service, state)
    except ValueError:
        return _redirect(service, False, "invalid_state")
    if error or not code or not service.settings.install_available:
        return _redirect(service, False, "authorization_denied")
    try:
        service.state_service.authorize_state_principal(record)
        service.credentials.authorize_organization(
            record.principal_id, Permission.ORGANIZATIONS_ADMIN
        )
    except PermissionError:
        return _redirect(service, False, "not_permitted")
    try:
        payload = _exchange(service, code)
    except SlackApiError:
        return _redirect(service, False, "token_exchange")
    try:
        install = parse_install(payload)
    except InstallRefused as refused:
        _revoke_quietly(service, token_of(payload))
        return _redirect(service, False, refused.reason)
    reason = _bind(service, record, install)
    return _redirect(service, reason is None, reason)


def _exchange(service: SlackInstallationService, code: str) -> dict[str, Any]:
    settings = service.settings
    return service.api.oauth_v2_access(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        code=code,
        redirect_uri=settings.redirect_uri,
    )


def _revoke_quietly(service: SlackInstallationService, token: str | None) -> None:
    if token is None:
        return
    try:
        service.api.auth_revoke(token)
    except Exception as error:
        logger.warning("slack_revoke_failed kind=%s", type(error).__name__)


def _bind(
    service: SlackInstallationService,
    record: OAuthAuthorizationState,
    install: BotInstall,
) -> str | None:
    """Store the token and bind the team; returns a refusal reason or ``None``."""
    try:
        ref = service.secrets.put(
            install.access_token, purpose=SECRET_PURPOSE, owner_ref=install.team_id
        )
    except Exception as error:
        logger.error("slack_secret_store_failed kind=%s", type(error).__name__)
        _revoke_quietly(service, install.access_token)
        return "secret_store_unavailable"
    now = service.clock()
    candidate = SlackInstallation(
        installation_id=new_id(),
        team_id=install.team_id,
        org_id=record.org_id,
        workspace_id=record.workspace_id,
        bot_user_id=install.bot_user_id,
        installed_by_principal_id=record.principal_id,
        installed_at=now,
        updated_at=now,
        status="active",
        team_name=install.team_name,
        enterprise_id=install.enterprise_id,
        app_id=install.app_id,
        bot_secret_ref=ref,
        scopes=install.scopes,
        installed_by_name=service.display_name(record.principal_id),
        installed_by_slack_user=install.slack_user,
    )
    try:
        result = service.store.claim(candidate)
    except Exception as error:
        logger.error("slack_claim_failed kind=%s", type(error).__name__)
        result = None
    if result is None or result.outcome == "conflict" or result.installation is None:
        service.secrets.delete(ref)
        _revoke_quietly(service, install.access_token)
        return "storage_failed" if result is None else "workspace_already_connected"
    if result.previous_secret_ref:
        # Reinstall by the same organization rotates the token: the old one goes.
        old = replace(result.installation, bot_secret_ref=result.previous_secret_ref)
        service.retire_token(old, revoke=False)
    service.audit(
        record.org_id,
        record.principal_id,
        f"slack.installation.{result.outcome}",
        result.installation,
        {"scopes": list(install.scopes), "enterprise": install.enterprise_id},
    )
    return None
