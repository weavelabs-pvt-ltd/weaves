"""``@Weaves review <PR link>``: pre-flight, then one review task as the mapped member.

Runs in the dispatcher (the fixed grammar already told this is a review with a
link). The engine's Execute pre-flight comes first and makes no connector call
and no model call: the pull request rule, the mapped active member holding the
Weaves permissions, the repository covered by an active GitHub connection and on
the admin's allowlist. A refusal is a private message (scene E) and one audit
record. A pass creates a task for the mapped member (so RBAC, limits and audit
follow them) whose only text is built from the validated link, with an origin that
pins the action and repository; the worker guard re-runs the pipeline at pickup
and approves exactly the pull request reader. Idempotent on the Slack event id.
"""

from __future__ import annotations

import logging
from datetime import datetime
from threading import Lock

from application.ports.slack import InboxEvent, SlackInstallation
from clients.integrations.github_pr_url import PullRequestRef, parse_pull_request_url
from contracts.v1 import TaskAudience, TurnOrigin
from contracts.v1.slack_threads import SlackThread
from modules.limits.errors import LimitExceeded
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.access.decision_audit import DecisionContext, DecisionRecorder
from modules.slack.access.models import Action, Decision, Outcome, Reason
from modules.slack.dispatch.ask_flow import (
    AskKind,
    AskResult,
    idempotency_key,
    request_id_of,
)
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.review.agent import (
    NoReviewModel,
    ReviewEnabler,
    ReviewProvisioner,
    ReviewUnavailable,
)
from modules.slack.review.book import ReviewBook
from modules.slack.review.prompt import review_task_text
from modules.tasks.service import TaskService
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack")


class ReviewFlow:
    """Gate, provision and queue one review; never calls GitHub, the model or Slack."""

    def __init__(
        self,
        *,
        gates: DispatchGates,
        provisioner: ReviewProvisioner,
        enabler: ReviewEnabler,
        tasks: TaskService,
        book: ReviewBook,
        recorder: DecisionRecorder,
    ) -> None:
        """Bind the gates, the agent and reader provisioning, tasks and rows."""
        self._gates = gates
        self._provisioner = provisioner
        self._enabler = enabler
        self._tasks = tasks
        self._book = book
        self._recorder = recorder
        self._lock = Lock()

    def run(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        link: str,
        now: datetime,
    ) -> AskResult:
        """The outcome of one review mention (``link`` is ``owner/repo#number``)."""
        owner_repo, _, number = link.partition("#")  # the classifier's "owner/repo#n"
        reference = parse_pull_request_url(
            f"https://github.com/{owner_repo}/pull/{number}"
        )
        if reference is None:
            return AskResult(AskKind.REFUSED, copy.MISSING_PR, "target_missing")
        channel, user = event.channel_id or "", event.user_id or ""
        decision = self._gates.evaluate(
            installation,
            slack_user_id=user,
            channel_id=channel,
            action=Action.PR_REVIEW,
            now=now,
            repo=reference.full_name,
        )
        if not decision.allowed:
            return self._blocked(installation, event, decision, now)
        member = self._gates.member_principal(user, channel)
        if member is None:  # the engine required a mapped member: never reached
            return AskResult(AskKind.REFUSED, messages.NOT_MAPPED, "member_not_mapped")
        thread_ts = event.thread_ts or event.message_ts or ""
        row = self._book.find(
            installation.org_id, installation.team_id, channel, thread_ts
        )
        if row is not None and row.event_id == event.event_id:
            return AskResult(AskKind.REPLAY)
        if row is not None and row.reply_state == "pending":
            return AskResult(AskKind.BUSY, reason="reply_pending")
        return self._start(installation, event, reference, member, row, now)

    # -- refusals -------------------------------------------------------------

    def _blocked(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        decision: Decision,
        now: datetime,
    ) -> AskResult:
        code = decision.reason.value if decision.reason else ""
        if decision.outcome is Outcome.DROPPED:
            return AskResult(AskKind.DROPPED, None, code)
        if decision.outcome is not Outcome.BLOCKED_RULE:
            return AskResult(
                AskKind.REFUSED, decision.message or messages.UNAVAILABLE, code
            )
        self._recorder.record(
            decision,
            DecisionContext(
                org_id=installation.org_id,
                event_id=event.event_id,
                request_id=request_id_of(event),
                slack_user_id=event.user_id or "",
                channel_id=event.channel_id or "",
            ),
            now,
        )
        text = (
            copy.NOT_SET_UP["review"]
            if decision.reason is Reason.RULE_OFF
            else decision.message or messages.UNAVAILABLE
        )
        return AskResult(
            AskKind.REFUSED, copy.refusal(text, Outcome.BLOCKED_RULE.value), code
        )

    # -- start ------------------------------------------------------------------

    def _start(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        reference: PullRequestRef,
        member: str,
        row: SlackThread | None,
        now: datetime,
    ) -> AskResult:
        org, workspace = installation.org_id, installation.workspace_id
        request_id = request_id_of(event)
        try:
            if not self._enabler.ensure(
                org, workspace, reference.full_name, request_id
            ):
                return AskResult(
                    AskKind.REFUSED, messages.REPO_NOT_ENABLED, "repo_not_covered"
                )
            agent_id = self._provisioner.ensure(org, workspace)
        except NoReviewModel:
            return AskResult(AskKind.NO_MODEL)
        except ReviewUnavailable:
            return AskResult(
                AskKind.UNAVAILABLE, messages.UNAVAILABLE, "review_unavailable"
            )
        token = set_request_identity(member, org, workspace)
        try:
            with self._lock, extend_context(request_id=request_id):
                return self._create(
                    installation, event, reference, member, agent_id, row, now
                )
        except LimitExceeded as error:
            text = (
                messages.rate_limited(error.retry_after_seconds)
                if error.retry_after_seconds
                else messages.UNAVAILABLE
            )
            return AskResult(AskKind.LIMITED, text, error.resource[:40])
        finally:
            reset_request_principal(token)

    def _create(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        reference: PullRequestRef,
        member: str,
        agent_id: str,
        row: SlackThread | None,
        now: datetime,
    ) -> AskResult:
        channel = event.channel_id or ""
        thread_ts = event.thread_ts or event.message_ts or ""
        origin = TurnOrigin(
            kind="slack",
            team_id=installation.team_id,
            channel_id=channel,
            thread_ts=thread_ts,
            slack_user_id=event.user_id or "",
            event_id=event.event_id,
            settings_version=self._gates.settings_version(),
            action="pr_review",
            repo=reference.full_name,
            pr_number=reference.number,
        )
        task = self._tasks.create_task(
            agent_id=agent_id,
            title=f"Review {reference.ref}",
            initial_message=review_task_text(reference.url),
            idempotency_key=idempotency_key(installation.team_id, event.event_id),
            audience=TaskAudience.PRIVATE,
            origin=origin,
        )
        stamp = utc_now()
        fresh = SlackThread(
            id=self._book.key(
                installation.org_id, installation.team_id, channel, thread_ts
            ),
            org_id=installation.org_id,
            workspace_id=installation.workspace_id,
            team_id=installation.team_id,
            channel_id=channel,
            thread_ts=thread_ts,
            kind="review",
            tier="execute",
            task_id=task.task_id,
            requester_slack_user_id=event.user_id or "",
            requester_principal_id=member,
            turn_id=task.active_turn_id or "",
            event_id=event.event_id,
            request_id=request_id_of(event),
            message_ts=event.message_ts or thread_ts,
            requested_at=now,
            created_at=stamp,
            updated_at=stamp,
        )
        if row is None:
            if self._book.start(fresh) is None:  # a concurrent dispatcher won
                return AskResult(AskKind.REPLAY)
            return AskResult(AskKind.CREATED)
        self._book.restart(row, fresh, stamp)
        return AskResult(AskKind.CREATED)
