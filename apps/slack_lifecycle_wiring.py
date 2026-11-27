"""Compose the Slack lifecycle and attach it to purge, export and the worker."""

from __future__ import annotations

import os
from typing import Any

from clients.slack.client import SlackApiClient
from modules.recovery.tombstones import tombstones_from_env
from modules.slack.export import installation_rows
from modules.slack.lifecycle import SlackLifecycle
from modules.slack.purge import SlackOrganizationPurge
from shared.slack_config import SlackConfig


def wire_slack_lifecycle(runtime: Any) -> SlackLifecycle:
    """Build the lifecycle once per process and hook it into the admin services.

    Only the API base URL is read from the environment: the signing secret is an
    API-only setting, so a worker must not need it.
    """
    config = SlackConfig.from_environment(
        {"WEAVES_SLACK_API_BASE_URL": os.environ.get("WEAVES_SLACK_API_BASE_URL", "")}
    )
    lifecycle = SlackLifecycle(
        store=runtime.slack_store,
        secrets=runtime.secret_store,
        api=SlackApiClient(runtime.http_client, api_base_url=config.api_base_url),
        audit_events=runtime.audit_events,
        tombstones=tombstones_from_env(),
    )
    runtime.slack_lifecycle = lifecycle
    runtime.organization_purge_service.slack = SlackOrganizationPurge(lifecycle)
    runtime.organization_export_service.slack_installations = lambda: installation_rows(
        runtime.slack_store
    )
    return lifecycle
