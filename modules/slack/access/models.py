"""Inputs and outputs of the Slack access policy engine (pure values)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from contracts.v1.slack_access import (
    AskLimits,
    ConnectorAudience,
    ExecuteLimits,
    ExecuteRules,
    SlackAccessSettings,
)


class Action(StrEnum):
    """What the dispatcher classified the Slack message as (code, never the model)."""

    ASK = "ask"
    ASK_PRIVATE = "ask_private"
    CREATE_TASK = "create_task"
    PR_REVIEW = "pr_review"
    RUN_WORKFLOW = "run_workflow"

    @property
    def is_execute(self) -> bool:
        """Tier 2: persists work, spends meaningful AI or writes."""
        return self in EXECUTE_ACTIONS

    @property
    def is_ask(self) -> bool:
        """Tier 1: read-only questions."""
        return self in (Action.ASK, Action.ASK_PRIVATE)


EXECUTE_ACTIONS = frozenset({Action.CREATE_TASK, Action.PR_REVIEW, Action.RUN_WORKFLOW})


class OrgState(StrEnum):
    """The organization's lifecycle standing as the dispatcher reads it."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


class InstallationState(StrEnum):
    """The Slack installation of the workspace the event came from."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    UNINSTALLED = "uninstalled"
    MISSING = "missing"


class ToolHealth(StrEnum):
    """Discovery status of one connector (only ``HEALTHY`` is green)."""

    HEALTHY = "healthy"
    ERROR = "error"
    DISABLED = "disabled"
    TOKEN_EXPIRED = "token_expired"
    STALE = "stale"  # stale snapshot whose live re-probe failed
    UNAVAILABLE = "unavailable"
    QUARANTINED = "quarantined"


class Mode(StrEnum):
    """What happens when a needed tool is red."""

    STRICT = "strict"
    PARTIAL = "partial"


class Outcome(StrEnum):
    """The decision outcome recorded on ``slack.decision``."""

    ALLOWED = "allowed"
    ALLOWED_PARTIAL = "allowed_partial"
    MODEL_ONLY = "model_only"
    REFUSED_ACCESS = "refused_access"
    REFUSED_UNAVAILABLE = "refused_unavailable"
    BLOCKED_RULE = "blocked_rule"
    BLOCKED_STATE = "blocked_state"
    PLANNER_FAILED = "planner_failed"
    RATE_LIMITED = "rate_limited"
    DROPPED = "dropped"

    @property
    def allows(self) -> bool:
        """Whether the request may proceed to the model run."""
        return self in (Outcome.ALLOWED, Outcome.ALLOWED_PARTIAL, Outcome.MODEL_ONLY)


class Reason(StrEnum):
    """Stable reason codes (audit and metrics; user text never exposes lists)."""

    SLACK_DISABLED = "slack_disabled"
    PLATFORM_PAUSED = "platform_paused"
    ORG_UNAVAILABLE = "org_unavailable"
    READ_ONLY = "read_only"
    INSTALLATION_INACTIVE = "installation_inactive"
    BOT_PAUSED = "bot_paused"
    QUESTION_TOO_LONG = "question_too_long"
    RATE_LIMIT_USER = "rate_limit_user"
    RATE_LIMIT_CHANNEL = "rate_limit_channel"
    RATE_LIMIT_ORG = "rate_limit_org"
    ASK_DISABLED = "ask_disabled"
    RULE_OFF = "rule_off"
    USER_NOT_LISTED = "user_not_listed"
    CHANNEL_NOT_LISTED = "channel_not_listed"
    MEMBER_NOT_MAPPED = "member_not_mapped"
    MEMBER_INACTIVE = "member_inactive"
    PERMISSION_MISSING = "permission_missing"
    TARGET_MISSING = "target_missing"
    REPO_NOT_COVERED = "repo_not_covered"
    REPO_NOT_ALLOWED = "repo_not_allowed"
    WORKFLOW_NOT_PUBLISHED = "workflow_not_published"
    WORKFLOW_NOT_ALLOWED = "workflow_not_allowed"
    AUDIENCE_NOT_IN_SLACK = "audience_not_in_slack"
    AUDIENCE_NOT_ALLOWED = "audience_not_allowed"
    TOOL_UNAVAILABLE = "tool_unavailable"
    TOOL_DISABLED = "tool_disabled"
    TOOL_TOKEN_EXPIRED = "tool_token_expired"
    TOOL_QUARANTINED = "tool_quarantined"
    PLANNER_FAILED = "planner_failed"


ACCESS_REASONS = frozenset({Reason.AUDIENCE_NOT_IN_SLACK, Reason.AUDIENCE_NOT_ALLOWED})


@dataclass(frozen=True)
class PlatformState:
    """Process and platform flags the dispatcher re-reads at every stage."""

    slack_enabled: bool = True
    admissions_paused: bool = False
    read_only: bool = False


@dataclass(frozen=True)
class PolicySettings:
    """The settings the engine reads (a detached view of the stored row)."""

    version: int = 0
    ask_enabled: bool = True
    connector_audience: Mapping[str, ConnectorAudience] = field(default_factory=dict)
    partial_answers: bool = False
    ask_limits: AskLimits = field(default_factory=AskLimits)
    rules: ExecuteRules = field(default_factory=ExecuteRules)
    execute_limits: ExecuteLimits = field(default_factory=ExecuteLimits)
    default_task_agent_id: str | None = None
    workflow_ids: frozenset[str] = frozenset()
    pr_review_repos: frozenset[str] = frozenset()
    paused: bool = False

    @classmethod
    def from_record(cls, record: SlackAccessSettings | None) -> PolicySettings:
        """Build the view; no stored row means the documented defaults."""
        if record is None:
            return cls()
        return cls(
            version=record.settings_version,
            ask_enabled=record.ask_enabled,
            connector_audience=dict(record.connector_audience),
            partial_answers=record.partial_answers,
            ask_limits=record.ask_limits,
            rules=record.rules,
            execute_limits=record.execute_limits,
            default_task_agent_id=record.default_task_agent_id,
            workflow_ids=frozenset(record.workflow_ids),
            pr_review_repos=frozenset(record.pr_review_repos),
            paused=record.paused,
        )

    def audience_of(self, installation_id: str) -> ConnectorAudience:
        """Per-connector audience; the default is ``not_in_slack``."""
        return self.connector_audience.get(installation_id, "not_in_slack")


@dataclass(frozen=True)
class MemberStanding:
    """A Weaves member a Slack user was mapped to, as the caller resolved them."""

    principal_id: str
    active: bool
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RepoTarget:
    """A pull request's repository and whether the GitHub installation covers it."""

    full_name: str
    covered: bool


@dataclass(frozen=True)
class WorkflowTarget:
    """The workflow a ``run`` request names."""

    workflow_id: str
    published: bool


@dataclass(frozen=True)
class NeededTool:
    """One connector the plan needs, with its discovery status."""

    tool_id: str
    installation_id: str
    label: str
    health: ToolHealth


@dataclass(frozen=True)
class AskUsage:
    """Timestamps of this org's counted Ask decisions (newest windows only)."""

    user: tuple[datetime, ...] = ()
    channel: tuple[datetime, ...] = ()
    org: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class ExecuteUsage:
    """Timestamps of this org's counted Execute decisions (newest window only)."""

    user: tuple[datetime, ...] = ()
    channel: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class AccessRequest:
    """Everything the engine needs; it performs no I/O and reads no clock."""

    action: Action
    now: datetime
    settings: PolicySettings
    slack_user_id: str
    channel_id: str
    # The asker's own standing on the allowlists (the lists are never passed in).
    user_listed: bool = False
    channel_listed: bool = False
    member: MemberStanding | None = None
    platform: PlatformState = field(default_factory=PlatformState)
    org_state: OrgState = OrgState.ACTIVE
    installation: InstallationState = InstallationState.ACTIVE
    repo: RepoTarget | None = None
    workflow: WorkflowTarget | None = None
    tools: tuple[NeededTool, ...] = ()
    question_chars: int = 0
    ask_usage: Callable[[], AskUsage] | None = None
    # Execute limits are checked only when the caller supplies the usage (the
    # dispatcher does; the pickup guard does not, so a request never counts itself).
    execute_usage: Callable[[], ExecuteUsage] | None = None


@dataclass(frozen=True)
class ToolResult:
    """The recorded health and access result of one needed tool."""

    tool_id: str
    installation_id: str
    label: str
    green: bool
    reason: Reason | None = None


@dataclass(frozen=True)
class Decision:
    """The engine's answer; ``message`` is user-facing and list-free."""

    allowed: bool
    outcome: Outcome
    action: Action
    mode: Mode
    settings_version: int
    reason: Reason | None = None
    message: str | None = None
    green_tools: tuple[str, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    skipped_note: str | None = None
    answer_ephemeral: bool = False
    retry_after_seconds: int | None = None
    checks: tuple[str, ...] = ()

    @property
    def skipped_tools(self) -> tuple[str, ...]:
        """Tool ids that were needed but red."""
        return tuple(r.tool_id for r in self.tool_results if not r.green)
