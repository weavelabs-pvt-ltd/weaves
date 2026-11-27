"""The worker's pre-run guard for Slack turns (design 3.5.2).

Settings, tokens, health and membership can change between the dispatcher and
the worker, so at pickup every Slack turn goes through the whole pipeline again
with the organization's current state. The guard turns the pipeline's result into
what the worker does and what the reply step reads:

* proceed: the turn runs with ``origin.capability_ids`` = needed AND green;
* the policy no longer passes (settings, pause, quarantine, uninstall, a
  deactivated member, a rate limit): the turn is cancelled with reason
  ``slack_access_revoked``;
* a needed tool is red or the plan failed: the turn finishes with the result's
  fixed short message and the answer model is never called.

``origin.verdict`` carries codes and fixed text only, never the question.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from application.ports.repository import Repository
from application.ports.slack import SlackStore
from application.ports.turn_guard import CANCEL_REASON_REVOKED, GuardDecision
from application.task_run_keys import planner_usage_key
from contracts.v1 import (
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    OrganizationLifecycleState,
    Task,
    TaskTurn,
    TurnOrigin,
    TurnVerdict,
)
from modules.slack.access import messages
from modules.slack.access.decision_audit import DecisionContext
from modules.slack.access.models import (
    AccessRequest,
    Action,
    InstallationState,
    OrgState,
    Outcome,
    PlatformState,
)
from modules.slack.access.ratelimit import AuditAskUsageReader
from modules.slack.access.service import SlackAccessService
from modules.slack.pipeline.models import PipelineRequest, PipelineResult
from modules.slack.pipeline.pipeline import SlackPipeline
from modules.slack.pipeline.targets import ExecuteTargets, PlannerTargets
from shared.clock import new_id, utc_now
from shared.metrics import SLACK_TURN_GUARD
from shared.run_origin import SLACK_KINDS

# Outcomes of the engine's gates: the request no longer passes policy.
_REVOKED = frozenset(
    {
        Outcome.DROPPED,
        Outcome.BLOCKED_STATE,
        Outcome.BLOCKED_RULE,
        Outcome.RATE_LIMITED,
    }
)
_ORG_STATES = {
    OrganizationLifecycleState.ACTIVE: OrgState.ACTIVE,
    OrganizationLifecycleState.SUSPENDED: OrgState.SUSPENDED,
}


class SlackTurnGuard:
    """Run the pipeline for a claimed Slack turn and decide what the worker does."""

    def __init__(
        self,
        *,
        pipeline: SlackPipeline,
        access: SlackAccessService,
        store: SlackStore,
        usage_reader: AuditAskUsageReader,
        audit_events: Repository[AuditEvent],
        planner_targets: PlannerTargets,
        execute_targets: ExecuteTargets,
        agent_versions: Repository[AgentVersion],
        slack_enabled: Callable[[], bool],
        admissions_paused: Callable[[], bool],
        read_only: Callable[[], bool],
        org_state: Callable[[str], OrganizationLifecycleState],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the pipeline and the state readers the engine's request needs."""
        self._pipeline = pipeline
        self._access = access
        self._store = store
        self._usage = usage_reader
        self._audit = audit_events
        self._planner_targets = planner_targets
        self._execute_targets = execute_targets
        self._agent_versions = agent_versions
        self._enabled = slack_enabled
        self._paused = admissions_paused
        self._read_only = read_only
        self._org_state = org_state
        self._clock = clock

    def check(
        self, turn: TaskTurn, task: Task, question: str, run_key: str
    ) -> GuardDecision | None:
        """The decision for a claimed turn, or ``None`` for a non-Slack turn."""
        origin = turn.origin
        if origin is None or origin.kind not in SLACK_KINDS:
            return None
        now = self._clock()
        request = self._request(turn, task, origin, question, run_key, now)
        result = self._pipeline.run_pipeline(request)
        decision = _decide(origin, result, now)
        if decision.cancel_reason is not None:
            self._audit_cancel(turn, origin, result, now)
        return decision

    def _audit_cancel(
        self, turn: TaskTurn, origin: TurnOrigin, result: PipelineResult, now: datetime
    ) -> None:
        """One audit record for a turn cancelled at pickup (codes, no text)."""
        event_id = new_id()
        reason = result.decision.reason.value if result.decision.reason else ""
        self._audit.create(
            AuditEvent(
                audit_event_id=event_id,
                org_id=turn.org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=f"slack:{origin.slack_user_id}",
                action="slack.turn.cancelled",
                target_type="task_turn",
                target_id=turn.turn_id,
                summary="Slack turn cancelled at pickup",
                request_id=result.request_id or event_id,
                metadata={
                    "cancel_reason": CANCEL_REASON_REVOKED,
                    "cause": reason,
                    "outcome": result.outcome.value,
                    "event_id": origin.event_id,
                    "settings_version": result.decision.settings_version,
                },
                created_at=now,
            )
        )

    def _request(
        self,
        turn: TaskTurn,
        task: Task,
        origin: TurnOrigin,
        question: str,
        run_key: str,
        now: datetime,
    ) -> PipelineRequest:
        settings = self._access.policy_settings()
        user_listed, channel_listed, member = self._access.asker_standing(
            origin.slack_user_id, origin.channel_id
        )
        action = Action(origin.action)
        context = DecisionContext(
            org_id=task.org_id,
            event_id=origin.event_id,
            request_id=turn.request_id or f"slack-{origin.event_id}",
            slack_user_id=origin.slack_user_id,
            channel_id=origin.channel_id,
            principal_id=member.principal_id if member else None,
        )
        access = AccessRequest(
            action=action,
            now=now,
            settings=settings,
            slack_user_id=origin.slack_user_id,
            channel_id=origin.channel_id,
            user_listed=user_listed,
            channel_listed=channel_listed,
            member=member,
            platform=PlatformState(
                slack_enabled=self._enabled(),
                admissions_paused=self._paused(),
                read_only=self._read_only(),
            ),
            org_state=_ORG_STATES.get(self._org_state(task.org_id), OrgState.DELETED),
            installation=self._installation_state(origin, task.org_id),
            repo=self._execute_targets.repo(
                origin.repo, task.org_id, task.workspace_id
            ),
            workflow=self._execute_targets.workflow(
                origin.workflow_id, task.org_id, task.workspace_id
            ),
            ask_usage=lambda: self._usage.load(
                task.org_id, origin.slack_user_id, origin.channel_id, now
            ),
        )
        return PipelineRequest(
            access=access,
            context=context,
            usage_key=planner_usage_key(run_key),
            question=question,
            planner_target=self._planner_targets.resolve(
                self._access.planner_profile_id(), task
            ),
            workspace_id=task.workspace_id,
            agent_capability_ids=self._agent_capabilities(task, action),
        )

    def _agent_capabilities(self, task: Task, action: Action) -> tuple[str, ...]:
        """The pinned agent version's capability ids (``create_task`` only)."""
        if action is not Action.CREATE_TASK:
            return ()
        try:
            version = self._agent_versions.get_scoped(
                task.agent_version_id, task.org_id, task.workspace_id
            )
        except KeyError:
            return ()
        return tuple(version.allowed_capability_ids)

    def _installation_state(self, origin: TurnOrigin, org_id: str) -> InstallationState:
        held = self._store.find_by_team(origin.team_id)
        if held is None or held.org_id != org_id:
            return InstallationState.MISSING
        return InstallationState(held.status)


def _decide(origin: TurnOrigin, result: PipelineResult, now: datetime) -> GuardDecision:
    """Map a pipeline result to the worker's action and the turn's new origin."""
    decision = result.decision
    refused = not result.proceed
    verdict = TurnVerdict(
        outcome=result.outcome.value,
        reason=decision.reason.value if decision.reason else None,
        message=result.message if refused else None,
        skipped_note=result.skipped_note,
        answer_ephemeral=result.answer_ephemeral or refused,
        tools_needed=result.tools_needed[:20],
        tools_green=result.capability_ids[:20],
        planner_status=result.planner.status.value,
        settings_version=decision.settings_version,
        decided_at=now,
    )
    revoked = refused and result.outcome in _REVOKED
    verdict = verdict.model_copy(
        update={"cancel_reason": CANCEL_REASON_REVOKED if revoked else None}
    )
    approved = result.capability_ids if result.proceed else ()
    updated = origin.model_copy(update={"capability_ids": approved, "verdict": verdict})
    if result.proceed:
        SLACK_TURN_GUARD.inc(outcome="proceeded")
        return GuardDecision(True, updated)
    if revoked:
        SLACK_TURN_GUARD.inc(outcome="cancelled")
        return GuardDecision(
            False, updated, cancel_reason=CANCEL_REASON_REVOKED, message=result.message
        )
    SLACK_TURN_GUARD.inc(outcome="refused")
    return GuardDecision(False, updated, message=result.message or messages.UNAVAILABLE)
