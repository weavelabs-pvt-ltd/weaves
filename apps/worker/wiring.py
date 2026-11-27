"""Construct the dependencies for the worker process."""

from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_wiring import open_process_runtime
from apps.worker.services import WorkerServices
from modules.executions.queue_health import QueueHealthService
from modules.slack.retention import run_tick as run_slack_lifecycle
from shared.limits_config import LimitsConfig


def build_platform_runtime() -> LocalPlatformRuntime:
    """Open worker-owned pools and compose the platform runtime."""
    return open_process_runtime(process="worker")


def build_worker_services(
    queue_age_warn_seconds: int = LimitsConfig().queue_age_warn_seconds,
) -> WorkerServices:
    """Build explicit worker dependencies and retain runtime-owned cleanup."""
    runtime = build_platform_runtime()
    return WorkerServices(
        storage_mode=runtime.storage_mode,
        bootstrap=runtime.bootstrap,
        close=runtime.close,
        execution=runtime.execution_worker_service,
        execution_recovery=runtime.recovery_service,
        workflow_automation=runtime.workflow_automation_service,
        tasks=runtime.task_worker_service,
        studio=runtime.workflow_studio_service,
        purge_accounts=runtime.account_sign_in_service.purge_expired,
        purge_organizations=runtime.organization_purge_service.tick,
        purge_waitlist=lambda: sum(runtime.waitlist_service.purge_stale()),
        queue_health=QueueHealthService(
            execution_jobs=runtime.execution_worker_service.execution_jobs,
            task_turns=runtime.task_worker_service.turns,
            warn_seconds=queue_age_warn_seconds,
        ),
        reconcile_usage=runtime.usage_reconciler.reconcile,
        purge_usage=runtime.usage_reconciler.purge_expired,
        slack_lifecycle=lambda: run_slack_lifecycle(runtime.slack_lifecycle),
        refresh_slack_health=(
            refresher.refresh_all
            if (refresher := runtime.slack_health_refresher) is not None
            else None
        ),
        dispatch_slack=(
            dispatcher.run if (dispatcher := runtime.slack_dispatcher) else None
        ),
        reply_slack=(
            reply_tick.run if (reply_tick := runtime.slack_reply_tick) else None
        ),
        interact_slack=(
            interactions.run if (interactions := runtime.slack_interactions) else None
        ),
        purge_slack=(retention.run if (retention := runtime.slack_retention) else None),
        feedback_slack=(
            feedback.run if (feedback := runtime.slack_feedback_tick) else None
        ),
        purge_slack_feedback=(
            expiry.run if (expiry := runtime.slack_feedback_retention) else None
        ),
        worker_paused=runtime.platform_controls.worker_paused,
    )
