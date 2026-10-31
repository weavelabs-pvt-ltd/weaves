"""Aggregate domain-specific runtime compatibility delegates."""

from application.runtime_auth_execution_compatibility import (
    RuntimeAuthExecutionCompatibilityMixin,
)
from application.runtime_identity_compatibility import (
    RuntimeIdentityCompatibilityMixin,
)
from application.runtime_workflow_compatibility import (
    RuntimeWorkflowCompatibilityMixin,
)


class RuntimeServiceCompatibilityMixin(
    RuntimeIdentityCompatibilityMixin,
    RuntimeAuthExecutionCompatibilityMixin,
    RuntimeWorkflowCompatibilityMixin,
):
    """Preserve the old runtime surface while services own capability behavior."""
