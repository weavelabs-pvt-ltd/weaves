"""The dispatcher's cheap gates: the S3 engine over the current platform state.

The same engine the worker guard runs at pickup (design 3.5.1 steps 1-5): platform
and organization state, the installation, the bot's own pause, Ask limits and the
tier rule. Run here it decides, before any task exists, whether a mention may
become one. It records nothing: Ask is counted from the decision the pipeline
writes at pickup, so a refusal at this stage never counts against anybody.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from application.ports.slack import SlackInstallation
from contracts.v1 import OrganizationLifecycleState
from modules.slack.access.engine import check_state, evaluate
from modules.slack.access.execute_limits import AuditExecuteUsageReader
from modules.slack.access.models import (
    AccessRequest,
    Action,
    Decision,
    InstallationState,
    MemberStanding,
    OrgState,
    PlatformState,
)
from modules.slack.access.ratelimit import AuditAskUsageReader
from modules.slack.access.service import SlackAccessService
from modules.slack.pipeline.targets import ExecuteTargets

_ORG_STATES = {
    OrganizationLifecycleState.ACTIVE: OrgState.ACTIVE,
    OrganizationLifecycleState.SUSPENDED: OrgState.SUSPENDED,
}


class DispatchGates:
    """Build the engine's request from live state and evaluate it."""

    def __init__(
        self,
        *,
        access: SlackAccessService,
        usage_reader: AuditAskUsageReader,
        execute_usage_reader: AuditExecuteUsageReader,
        execute_targets: ExecuteTargets,
        slack_enabled: Callable[[], bool],
        admissions_paused: Callable[[], bool],
        read_only: Callable[[], bool],
        org_state: Callable[[str], OrganizationLifecycleState],
    ) -> None:
        """Bind the state readers (the same ones the worker guard uses)."""
        self._access = access
        self._usage = usage_reader
        self._execute_usage = execute_usage_reader
        self._targets = execute_targets
        self._enabled = slack_enabled
        self._paused = admissions_paused
        self._read_only = read_only
        self._org_state = org_state

    def state(
        self,
        installation: SlackInstallation,
        *,
        slack_user_id: str,
        channel_id: str,
        now: datetime,
    ) -> Decision | None:
        """``None`` when the bot may speak; else the state gate's refusal."""
        return check_state(
            self._request(
                installation,
                slack_user_id=slack_user_id,
                channel_id=channel_id,
                action=Action.ASK,
                now=now,
            )
        )

    def evaluate(
        self,
        installation: SlackInstallation,
        *,
        slack_user_id: str,
        channel_id: str,
        action: Action,
        now: datetime,
        question_chars: int = 0,
        repo: str | None = None,
        event_id: str | None = None,
    ) -> Decision:
        """The engine's decision for this asker and action with no tools needed.

        ``event_id`` is the request being decided: Execute limits never count it.
        """
        return evaluate(
            self._request(
                installation,
                slack_user_id=slack_user_id,
                channel_id=channel_id,
                action=action,
                now=now,
                question_chars=question_chars,
                repo=repo,
                event_id=event_id,
            )
        )

    def _request(
        self,
        installation: SlackInstallation,
        *,
        slack_user_id: str,
        channel_id: str,
        action: Action,
        now: datetime,
        question_chars: int = 0,
        repo: str | None = None,
        event_id: str | None = None,
    ) -> AccessRequest:
        user_listed, channel_listed, member = self._access.asker_standing(
            slack_user_id, channel_id
        )
        org_id = installation.org_id
        return AccessRequest(
            action=action,
            now=now,
            settings=self._access.policy_settings(),
            slack_user_id=slack_user_id,
            channel_id=channel_id,
            user_listed=user_listed,
            channel_listed=channel_listed,
            member=member,
            platform=PlatformState(
                slack_enabled=self._enabled(),
                admissions_paused=self._paused(),
                read_only=self._read_only(),
            ),
            org_state=_ORG_STATES.get(self._org_state(org_id), OrgState.DELETED),
            installation=InstallationState(installation.status),
            repo=self._targets.repo(repo, org_id, installation.workspace_id),
            question_chars=question_chars,
            ask_usage=lambda: self._usage.load(org_id, slack_user_id, channel_id, now),
            execute_usage=(
                (
                    lambda: self._execute_usage.load(
                        org_id,
                        slack_user_id,
                        channel_id,
                        now,
                        exclude_event_id=event_id,
                    )
                )
                if action.is_execute
                else None
            ),
        )

    def member(self, slack_user_id: str, channel_id: str) -> MemberStanding | None:
        """The Weaves member the asker was mapped to (``None``: unmapped)."""
        return self._access.asker_standing(slack_user_id, channel_id)[2]

    def default_task_agent_id(self) -> str | None:
        """The agent that runs ``task:`` requests, if an admin chose one."""
        return self._access.policy_settings().default_task_agent_id

    def settings_version(self) -> int:
        """The settings version a request is created under."""
        return self._access.policy_settings().version

    def rule_for(self, action: str) -> str:
        """The Execute rule name of ``action`` (``run_workflow`` and friends)."""
        return str(getattr(self._access.policy_settings().rules, action, "off"))

    def standing(self, slack_user_id: str, channel_id: str) -> tuple[bool, bool]:
        """Whether the asker and the channel are on the allowlists (two booleans)."""
        user, channel, _ = self._access.asker_standing(slack_user_id, channel_id)
        return user, channel
