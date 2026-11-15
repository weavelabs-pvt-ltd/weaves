"""Explicit connected-tool fixtures for runtime tests after knowledge removal."""

import json

import httpx


def connect_slack(runtime):
    """Connect a read-only Slack capability backed by a deterministic HTTP stub."""
    runtime.bootstrap()
    runtime.slack_plugin._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"ok": True, "messages": {"matches": []}}
            )
        )
    )
    runtime.integration_connection_service.create_slack_oauth_installation(
        display_name="Test Slack",
        token_bundle=json.dumps(
            {"weaves_oauth": True, "provider": "slack", "access_token": "fixture-only"}
        ),
        channels=("testing",),
    )
    return "slack.messages.search"
