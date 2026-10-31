"""Public queue runtime composed from operation and worker capabilities."""

from modules.evaluations.runtime_queue_operations import EvaluationQueueOperationsMixin
from modules.evaluations.runtime_queue_worker import EvaluationQueueWorkerMixin


class EvaluationQueueRuntimeMixin(
    EvaluationQueueOperationsMixin,
    EvaluationQueueWorkerMixin,
):
    """Compose durable evaluation queue operations and worker behavior."""
