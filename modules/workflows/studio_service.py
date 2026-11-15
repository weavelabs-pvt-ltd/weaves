"""Compose typed publication, execution and scheduling of durable canvas runs."""

from modules.workflows.studio_execution import WorkflowStudioExecutionMixin
from modules.workflows.studio_publication import WorkflowStudioPublicationMixin
from modules.workflows.studio_scheduling import WorkflowStudioSchedulingMixin


class WorkflowStudioService(
    WorkflowStudioPublicationMixin,
    WorkflowStudioExecutionMixin,
    WorkflowStudioSchedulingMixin,
):
    """Publish canvas plans and advance them through durable agent tasks."""
