"""Two-organization world with a Slack-ready runtime for access API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.v1 import PluginInstallation, PluginInstallationStatus
from tests.support.organization_administration import prepare, scope

BASE = "/api/v0/organization/slack/access"
ASKER = "U0ASKER01"
OTHER = "U0OTHER02"
CHANNEL = "C0CHAN001"


def install_connector(
    runtime: Any, owner: tuple[Any, ...], installation_id: str
) -> None:
    """Create a plugin installation row for an organization's workspace."""
    now = datetime.now(UTC)
    with scope(owner):
        runtime.installations.put(
            PluginInstallation(
                plugin_installation_id=installation_id,
                org_id=owner[0].id,
                workspace_id=owner[1].id,
                plugin_id="jira",
                display_name="Jira",
                status=PluginInstallationStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )


def build_world(runtime: Any) -> dict[str, Any]:
    """Two owners, a Builder member in each, and one connector in Org A."""
    world = {"runtime": runtime, **prepare(runtime)}
    install_connector(runtime, world["a"], "inst-jira")
    return world


def settings_body(version: int, **changes: Any) -> dict[str, Any]:
    """A complete, valid settings replacement."""
    body: dict[str, Any] = {
        "expected_version": version,
        "ask_enabled": True,
        "connector_audience": {},
        "partial_answers": False,
        "planner_profile_id": None,
        "ask_limits": {
            "user_per_10_min": 5,
            "channel_per_10_min": 20,
            "org_per_day": 200,
            "max_question_chars": 1000,
        },
        "rules": {
            "create_task": "off",
            "pr_review": "off",
            "run_workflow": "off",
            "ask_private": "off",
        },
        "workflow_ids": [],
        "pr_review_repos": [],
    }
    body.update(changes)
    return body
