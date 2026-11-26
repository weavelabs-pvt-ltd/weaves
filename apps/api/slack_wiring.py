"""Compose the Slack bot's API services from the process runtime."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI

from apps.platform_runtime import LocalPlatformRuntime
from clients.slack.client import SlackApiClient
from contracts.v1 import OrganizationLifecycleState
from modules.integrations.oauth_state_service import OAuthStateService
from modules.slack.events import SlackEventIngest
from modules.slack.http.events_route import SlackEventsEndpoint, register_events_route
from modules.slack.http.install_routes import register_install_routes
from modules.slack.install_settings import SlackInstallSettings
from modules.slack.installation_service import SlackInstallationService
from modules.slack.reactions import AckReaction
from shared.request_context import effective_org_id


@dataclass(frozen=True)
class SlackRouteServices:
    """The install service, event ingest and acknowledgement reaction."""

    settings: SlackInstallSettings
    installs: SlackInstallationService
    ingest: SlackEventIngest
    react: AckReaction


def _display_name(runtime: LocalPlatformRuntime, principal_id: str) -> str | None:
    """The installer's display name, or ``None`` when it cannot be resolved."""
    try:
        org_id = effective_org_id()
        principal = runtime.principals.get_scoped(principal_id, org_id)
        if principal.user_id is None:
            return None
        return str(runtime.users.get_scoped(principal.user_id, org_id).display_name)
    except Exception:
        return None


def build_slack_route_services(runtime: LocalPlatformRuntime) -> SlackRouteServices:
    """Read the Slack settings once and wire the services (dark unless enabled)."""
    settings = SlackInstallSettings.from_environment()
    api = SlackApiClient(runtime.http_client, api_base_url=settings.config.api_base_url)
    lifecycle = runtime.organization_lifecycle_service
    installs = SlackInstallationService(
        store=runtime.slack_store,
        secrets=runtime.secret_store,
        state_service=OAuthStateService(
            states=runtime.oauth_authorization_states,
            credential_service=runtime.credential_service,
            secret_store=runtime.secret_store,
        ),
        credentials=runtime.credential_service,
        api=api,
        settings=settings,
        display_name=lambda pid: _display_name(runtime, pid),
    )
    ingest = SlackEventIngest(
        store=runtime.slack_store,
        installs=installs,
        enabled=settings.config.enabled,
        platform_paused=lambda: runtime.platform_controls.snapshot().admissions_paused,
        org_active=lambda org_id: (
            lifecycle.state_of(org_id) is OrganizationLifecycleState.ACTIVE
        ),
    )
    return SlackRouteServices(
        settings=settings,
        installs=installs,
        ingest=ingest,
        react=AckReaction(api, runtime.secret_store, installs),
    )


def register_slack_routes(app: FastAPI, services: SlackRouteServices) -> None:
    """Install flow, status and the signed Events endpoint."""
    register_install_routes(app, services.installs)
    register_events_route(
        app,
        SlackEventsEndpoint(
            signing_secret=services.settings.config.signing_secret,
            ingest=services.ingest,
            react=services.react,
        ),
    )


__all__ = ["SlackRouteServices", "build_slack_route_services", "register_slack_routes"]
