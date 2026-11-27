"""Compose the Slack dispatcher, reply tick and retention tick for the worker.

Runs after the pipeline is attached. Everything is reachable only from the worker
(``WorkerServices``); the API never posts an answer. The Slack Web API client uses
``WEAVES_SLACK_API_BASE_URL`` (the real ``https://slack.com/api`` unless a harness
points it at the fake) and the bot token is read from the secret store per post.
"""

from __future__ import annotations

from datetime import timedelta

from apps.platform_runtime import LocalPlatformRuntime
from clients.slack.client import SlackApiClient
from contracts.v1 import OrganizationLifecycleState
from modules.models.defaults import default_active_model_profile
from modules.slack.access.decision_audit import DecisionRecorder
from modules.slack.access.ratelimit import AuditAskUsageReader
from modules.slack.dispatch.ask_flow import AskFlow
from modules.slack.dispatch.cheap import CheapExits
from modules.slack.dispatch.dispatcher import SlackDispatcher
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.dispatch.qa_agent import QaProvisioner
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.pipeline.discovery import ToolDiscovery
from modules.slack.pipeline.health import HealthSnapshots
from modules.slack.pipeline.targets import ExecuteTargets
from modules.slack.qa_retention import SlackRetention, qa_retention_days
from modules.slack.reply.deliver import Delivery
from modules.slack.reply.loss import InstallationLoss
from modules.slack.reply.poster import SlackPoster
from modules.slack.reply.steps import RunSteps
from modules.slack.reply.tick import SlackReplyTick
from modules.slack.review.agent import ReviewEnabler, ReviewProvisioner
from modules.slack.review.book import ReviewBook
from modules.slack.review.flow import ReviewFlow
from modules.slack.review.reply import ReviewReply
from modules.slack.review.stats import ReviewProgress
from shared.slack_config import (
    native_status_from_environment,
    reply_deadline_seconds_from_environment,
    slack_api_base_url_from_environment,
    slack_enabled_from_environment,
)


def _gates(runtime: LocalPlatformRuntime) -> DispatchGates:
    controls = runtime.platform_controls
    return DispatchGates(
        access=runtime.slack_access_service,
        usage_reader=AuditAskUsageReader(runtime.audit_events),
        execute_targets=ExecuteTargets(
            installations=runtime.installations, workflows=runtime.workflows
        ),
        slack_enabled=slack_enabled_from_environment,
        admissions_paused=lambda: controls.snapshot().admissions_paused,
        read_only=lambda: controls.snapshot().read_only,
        org_state=lambda org_id: runtime.organization_lifecycle_service.state_of(
            org_id
        ),
    )


def _provisioner(runtime: LocalPlatformRuntime) -> QaProvisioner:
    snapshots = HealthSnapshots(
        runtime.slack_connector_health,
        runtime.integration_admin_service.probe_plugin_installation,
    )
    return QaProvisioner(
        access=runtime.slack_access_service,
        principals=runtime.principals,
        roles=runtime.roles,
        role_bindings=runtime.role_bindings,
        agents=runtime.agents,
        agent_versions=runtime.agent_versions,
        agent_service=runtime.agent_service,
        model_profiles=runtime.model_profiles,
        discovery=ToolDiscovery(
            installations=runtime.installations,
            plugins=runtime.plugins,
            snapshots=snapshots,
        ),
        default_profile=lambda: default_active_model_profile(
            runtime.model_profiles, runtime.model_configurations
        ),
    )


def _review_flow(runtime: LocalPlatformRuntime, gates: DispatchGates) -> ReviewFlow:
    return ReviewFlow(
        gates=gates,
        provisioner=ReviewProvisioner(
            agents=runtime.agents,
            agent_versions=runtime.agent_versions,
            agent_service=runtime.agent_service,
            model_profiles=runtime.model_profiles,
            default_profile=lambda: default_active_model_profile(
                runtime.model_profiles, runtime.model_configurations
            ),
            mutation=runtime.slack_access_service.mutation,
        ),
        enabler=ReviewEnabler(
            installations=runtime.installations, audit_events=runtime.audit_events
        ),
        tasks=runtime.task_service,
        book=ReviewBook(runtime.slack_threads),
        recorder=DecisionRecorder(runtime.audit_events),
    )


def _reply_tick(
    runtime: LocalPlatformRuntime,
    book: ThreadBook,
    delivery: Delivery,
    loss: InstallationLoss,
) -> SlackReplyTick:
    lifecycle = runtime.organization_lifecycle_service
    return SlackReplyTick(
        store=runtime.slack_store,
        threads=runtime.slack_threads,
        turns=runtime.task_turns,
        task_messages=runtime.task_messages,
        agent_runs=runtime.agent_runs,
        book=book,
        steps=RunSteps(
            runs=runtime.runs,
            invocations=runtime.tool_invocations,
            installations=runtime.installations,
        ),
        delivery=delivery,
        loss=loss,
        enabled=slack_enabled_from_environment,
        org_active=lambda org_id: (
            lifecycle.state_of(org_id) is OrganizationLifecycleState.ACTIVE
        ),
        unit_of_work=runtime._unit_of_work,
        lock=runtime._advisory_transaction_lock,
        native_status=native_status_from_environment,
        kinds={
            "review": ReviewReply(
                progress=ReviewProgress(
                    runs=runtime.runs, invocations=runtime.tool_invocations
                ),
                task_messages=runtime.task_messages,
            )
        },
        deadline=timedelta(seconds=reply_deadline_seconds_from_environment()),
    )


def _retention(runtime: LocalPlatformRuntime) -> SlackRetention:
    return SlackRetention(
        store=runtime.slack_store,
        threads=runtime.slack_threads,
        tasks=runtime.tasks,
        conversation_threads=runtime.threads,
        messages=runtime.task_messages,
        turns=runtime.task_turns,
        agent_runs=runtime.agent_runs,
        runs=runtime.runs,
        invocations=runtime.tool_invocations,
        artifacts=runtime.artifacts,
        unit_of_work=runtime._unit_of_work,
        enabled=slack_enabled_from_environment,
        qa_days=qa_retention_days(),
    )


def attach_slack_dispatch(
    runtime: LocalPlatformRuntime, api: SlackApiClient | None = None
) -> None:
    """Build the three Slack worker ticks once and hang them on the runtime.

    ``api`` lets a test (or the harness) supply its own Slack client; by default
    the client uses the process HTTP client and ``WEAVES_SLACK_API_BASE_URL``.
    """
    gates = _gates(runtime)
    book = ThreadBook(runtime.slack_threads)
    api = api or SlackApiClient(
        runtime.http_client, api_base_url=slack_api_base_url_from_environment()
    )
    poster = SlackPoster(api, runtime.secret_store)
    loss = InstallationLoss(
        store=runtime.slack_store,
        secrets=runtime.secret_store,
        audit_events=runtime.audit_events,
    )
    runtime.slack_dispatcher = SlackDispatcher(
        store=runtime.slack_store,
        gates=gates,
        ask=AskFlow(
            gates=gates,
            qa=_provisioner(runtime),
            tasks=runtime.task_service,
            book=book,
        ),
        cheap=CheapExits(poster=poster, book=book, gates=gates),
        review=_review_flow(runtime, gates),
        loss=loss,
        enabled=slack_enabled_from_environment,
    )
    runtime.slack_reply_tick = _reply_tick(runtime, book, Delivery(poster, book), loss)
    runtime.slack_retention = _retention(runtime)
