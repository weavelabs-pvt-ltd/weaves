"""Stored Slack access settings, allowlists and the pure policy vocabulary.

Three organization-scoped collections back the dashboard's "Slack access" page:
one settings row per organization (record id = ``org_id``), the allowed Slack
people (each optionally mapped by an administrator to a Weaves member) and the
allowed channels. Nothing here holds Slack message text or tokens.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from contracts.v1.base import MutableOrgScopedContract, OpaqueId

SlackRule = Literal[
    "off", "user_only", "channel_only", "user_and_channel", "user_or_channel"
]
ConnectorAudience = Literal["everyone", "allowed_people", "not_in_slack"]
ExecuteAction = Literal["create_task", "pr_review", "run_workflow"]

SLACK_USER_ID_PATTERN = r"^[UW][A-Z0-9]{2,31}$"
SLACK_CHANNEL_ID_PATTERN = r"^[CG][A-Z0-9]{2,31}$"
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}$")

SlackUserId = Annotated[str, StringConstraints(pattern=SLACK_USER_ID_PATTERN)]
SlackChannelId = Annotated[str, StringConstraints(pattern=SLACK_CHANNEL_ID_PATTERN)]

MAX_ALLOWED_PEOPLE = 500
MAX_ALLOWED_CHANNELS = 500
MAX_ALLOWED_REPOS = 500
MAX_ALLOWED_WORKFLOWS = 200
MAX_CONNECTORS = 100


class _Strict(BaseModel):
    """Request and value objects reject undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class AskLimits(_Strict):
    """How much open Ask may spend (the organization's own AI key pays)."""

    user_per_10_min: int = Field(default=5, ge=1, le=1000)
    channel_per_10_min: int = Field(default=20, ge=1, le=10000)
    org_per_day: int = Field(default=200, ge=1, le=5000)
    max_question_chars: int = Field(default=1000, ge=50, le=4000)


class ExecuteRules(_Strict):
    """One rule per action; every action is off until an admin turns it on."""

    create_task: SlackRule = "off"
    pr_review: SlackRule = "off"
    run_workflow: SlackRule = "off"
    ask_private: SlackRule = "off"


class SlackSettingsFields(_Strict):
    """The administrator-editable part of the settings (a complete replacement)."""

    ask_enabled: bool = True
    connector_audience: dict[OpaqueId, ConnectorAudience] = Field(
        default_factory=dict, max_length=MAX_CONNECTORS
    )
    partial_answers: bool = False
    planner_profile_id: OpaqueId | None = None
    ask_limits: AskLimits = Field(default_factory=AskLimits)
    rules: ExecuteRules = Field(default_factory=ExecuteRules)
    workflow_ids: list[OpaqueId] = Field(
        default_factory=list, max_length=MAX_ALLOWED_WORKFLOWS
    )
    pr_review_repos: list[str] = Field(
        default_factory=list, max_length=MAX_ALLOWED_REPOS
    )

    @field_validator("pr_review_repos")
    @classmethod
    def repos_are_owner_slash_name(cls, value: list[str]) -> list[str]:
        """Normalize to unique lower-case ``owner/name`` entries."""
        cleaned: list[str] = []
        for item in value:
            repo = item.strip().lower()
            if not _REPO.fullmatch(repo):
                raise ValueError("repositories must look like owner/name")
            if repo not in cleaned:
                cleaned.append(repo)
        return cleaned

    @field_validator("workflow_ids")
    @classmethod
    def workflows_are_unique(cls, value: list[str]) -> list[str]:
        """Keep the first occurrence of each workflow id."""
        return list(dict.fromkeys(value))


class SlackAccessSettings(MutableOrgScopedContract):
    """The single settings row of an organization; ``org_id`` is the record id."""

    settings_version: int = Field(ge=1)
    ask_enabled: bool = True
    connector_audience: dict[OpaqueId, ConnectorAudience] = Field(default_factory=dict)
    partial_answers: bool = False
    planner_profile_id: OpaqueId | None = None
    ask_limits: AskLimits = Field(default_factory=AskLimits)
    rules: ExecuteRules = Field(default_factory=ExecuteRules)
    workflow_ids: list[OpaqueId] = Field(default_factory=list)
    pr_review_repos: list[str] = Field(default_factory=list)
    # Set by the Ask slice when it provisions the Q&A service principal.
    qa_principal_id: OpaqueId | None = None
    qa_agent_version_id: OpaqueId | None = None
    paused: bool = False
    updated_by: str = Field(default="", max_length=320)


class SlackAllowedUser(MutableOrgScopedContract):
    """A Slack member on the allowlist, optionally mapped to a Weaves member.

    The record id is ``{org_id}:{slack_user_id}`` and the pair is also unique in
    the database. ``principal_id`` is asserted by an administrator only.
    """

    id: OpaqueId
    slack_user_id: SlackUserId
    principal_id: OpaqueId | None = None
    note: str = Field(default="", max_length=200)
    added_by: OpaqueId


class SlackAllowedChannel(MutableOrgScopedContract):
    """A Slack channel on the allowlist (the record id is ``{org_id}:{channel}``)."""

    id: OpaqueId
    channel_id: SlackChannelId
    name_cached: str = Field(default="", max_length=120)
    added_by: OpaqueId


ConnectorHealthStatus = Literal[
    "healthy", "error", "token_expired", "rate_limited", "disabled"
]


class SlackConnectorHealth(MutableOrgScopedContract):
    """The latest health snapshot of one connector installation (a cache).

    The record id is the plugin installation id. The ``slack_health`` worker tick
    refreshes it every ten minutes; it holds a status and a reason code, never
    a token or connector data.
    """

    id: OpaqueId
    installation_id: OpaqueId
    status: ConnectorHealthStatus
    reason_code: str = Field(default="", max_length=64)
    checked_at: AwareDatetime


def allowed_user_record_id(org_id: str, slack_user_id: str) -> str:
    """Return the stable record id of an allowlisted Slack user."""
    return f"{org_id}:{slack_user_id}"


def allowed_channel_record_id(org_id: str, channel_id: str) -> str:
    """Return the stable record id of an allowlisted Slack channel."""
    return f"{org_id}:{channel_id}"


class SlackSettingsUpdate(SlackSettingsFields):
    """A complete settings replacement against a known version."""

    expected_version: int = Field(ge=0)


class SlackPauseRequest(_Strict):
    """Pause or resume the bot (honoured even when the settings changed)."""

    paused: bool


class SlackAllowedUserRequest(_Strict):
    """Allow a Slack member, optionally asserting which Weaves member they are."""

    slack_user_id: SlackUserId
    principal_id: OpaqueId | None = None
    note: str = Field(default="", max_length=200)


class SlackAllowedChannelRequest(_Strict):
    """Allow a Slack channel by id (the name is only a display hint)."""

    channel_id: SlackChannelId
    name_cached: str = Field(default="", max_length=120)


class SlackAccessView(SlackSettingsFields):
    """What ``GET /organization/slack/access`` returns."""

    settings_version: int = Field(ge=0)
    paused: bool
    updated_by: str
    updated_at: str | None
    allowed_people_count: int = Field(ge=0)
    allowed_channel_count: int = Field(ge=0)
