"""Compose the Slack reaction feedback tick and its retention tick (S4c).

Worker-only, like the other Slack ticks. The feedback tick reads pending
``reaction_added`` inbox rows through the same state gate as the dispatcher; the
retention tick deletes expired ratings through the system-purpose admin store.
"""

from __future__ import annotations

from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_services_slack_dispatch import _gates
from modules.slack.feedback.asker_ref import AskerRefs
from modules.slack.feedback.processor import SlackFeedbackTick
from modules.slack.feedback.retention import FeedbackRetention
from shared.slack_config import slack_enabled_from_environment


def attach_slack_feedback(runtime: LocalPlatformRuntime) -> None:
    """Build the two feedback ticks once and hang them on the runtime."""
    controls = runtime.platform_controls
    runtime.slack_feedback_tick = SlackFeedbackTick(
        store=runtime.slack_store,
        gates=_gates(runtime),
        threads=runtime.slack_threads,
        turns=runtime.task_turns,
        agent_runs=runtime.agent_runs,
        feedback=runtime.slack_feedback,
        refs=AskerRefs(
            access=runtime.slack_access_service, secrets=runtime.secret_store
        ),
        enabled=slack_enabled_from_environment,
        read_only=lambda: controls.snapshot().read_only,
    )
    runtime.slack_feedback_retention = FeedbackRetention(
        runtime.slack_feedback_admin, days=runtime.usage_settings.retention_days
    )
