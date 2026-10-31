"""Public evaluation runtime mixin assembled from focused capabilities."""

from modules.evaluations.runtime_queue import EvaluationQueueRuntimeMixin
from modules.evaluations.runtime_reports import EvaluationReportRuntimeMixin
from modules.evaluations.runtime_suites import EvaluationSuiteRuntimeMixin


class EvaluationRuntimeMixin(
    EvaluationQueueRuntimeMixin,
    EvaluationReportRuntimeMixin,
    EvaluationSuiteRuntimeMixin,
):
    """Compose evaluation suite, report, and durable queue operations."""
