"""Compose the Slack request pipeline, the worker's pre-run guard and health tick.

Runs after every capability service exists: the guard is attached to the task
worker (it re-checks Slack turns at pickup) and the refresher is exposed for the
worker's ``slack_health`` tick. Nothing here is reachable from the API.
"""

from __future__ import annotations

from apps.platform_runtime import LocalPlatformRuntime
from modules.slack.access.decision_audit import DecisionRecorder
from modules.slack.access.ratelimit import AuditAskUsageReader
from modules.slack.pipeline.discovery import ToolDiscovery
from modules.slack.pipeline.guard import SlackTurnGuard
from modules.slack.pipeline.health import HealthSnapshots, SlackHealthRefresher
from modules.slack.pipeline.pipeline import SlackPipeline
from modules.slack.pipeline.planner import AskPlanner
from modules.slack.pipeline.targets import ExecuteTargets, PlannerTargets
from shared.slack_config import slack_enabled_from_environment


def attach_slack_pipeline(runtime: LocalPlatformRuntime) -> None:
    """Build the pipeline once and attach the guard and the health refresher."""
    snapshots = HealthSnapshots(
        runtime.slack_connector_health,
        runtime.integration_admin_service.probe_plugin_installation,
    )
    pipeline = SlackPipeline(
        discovery=ToolDiscovery(
            installations=runtime.installations,
            plugins=runtime.plugins,
            snapshots=snapshots,
        ),
        planner=AskPlanner(runtime.model_gateway, runtime.usage_service),
        recorder=DecisionRecorder(runtime.audit_events),
    )
    controls = runtime.platform_controls
    lifecycle = runtime.organization_lifecycle_service
    runtime.task_worker_service.turn_guard = SlackTurnGuard(
        pipeline=pipeline,
        access=runtime.slack_access_service,
        store=runtime.slack_store,
        usage_reader=AuditAskUsageReader(runtime.audit_events),
        audit_events=runtime.audit_events,
        planner_targets=PlannerTargets(
            agent_versions=runtime.agent_versions,
            model_profiles=runtime.model_profiles,
            providers=runtime.providers,
            model_configurations=runtime.model_configurations,
        ),
        execute_targets=ExecuteTargets(
            installations=runtime.installations, workflows=runtime.workflows
        ),
        slack_enabled=slack_enabled_from_environment,
        admissions_paused=lambda: controls.snapshot().admissions_paused,
        read_only=lambda: controls.snapshot().read_only,
        org_state=lifecycle.state_of,
    )
    runtime.slack_health_refresher = SlackHealthRefresher(
        store=runtime.slack_store,
        installations=runtime.installations,
        snapshots=snapshots,
        enabled=slack_enabled_from_environment,
    )
