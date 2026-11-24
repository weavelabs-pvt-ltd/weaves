"""What a task owner is told when a task turn fails.

Only failures whose type guarantees a user-safe message are described specifically;
anything else gets the generic summary, and its details stay in the operator log.
"""

from __future__ import annotations

from pydantic import ValidationError

from contracts.v1 import ErrorSummary
from modules.agents.runtime_state import (
    AgentRuntimeBudgetExceeded,
    RunCostBudgetExceeded,
)
from shared.errors import ConfigurationRejected, ModelGatewayError

GENERIC_FAILURE_CODE = "task.agent_run_failed"
RUNTIME_LIMIT_CODE = "task.agent_runtime_limit_reached"
COST_LIMIT_CODE = "task.agent_cost_limit_reached"

_GENERIC_FAILURE = ErrorSummary(
    code=GENERIC_FAILURE_CODE,
    summary="The agent could not complete this task turn.",
    retryable=True,
)
# A task keeps the agent version it started with, so fixing the agent does not
# change what a retry of this task runs.
_START_A_NEW_TASK = (
    " This task keeps the agent version it started with, so start a new task "
    "once that is fixed."
)


def turn_failure_summary(error: BaseException) -> ErrorSummary:
    """Return the user-safe summary to store on a failed task turn."""
    try:
        return _specific_summary(error) or _GENERIC_FAILURE
    except ValidationError:
        # A message that fails ErrorSummary's own safety checks is not shown.
        return _GENERIC_FAILURE


def is_expected_failure(summary: ErrorSummary) -> bool:
    """Whether the failure is a known, user-correctable outcome (no stack needed)."""
    return summary.code != GENERIC_FAILURE_CODE


def _specific_summary(error: BaseException) -> ErrorSummary | None:
    if isinstance(error, ConfigurationRejected):
        return ErrorSummary(
            code=error.code,
            summary=_bounded(error.message + _START_A_NEW_TASK),
            retryable=False,
        )
    if isinstance(error, AgentRuntimeBudgetExceeded):
        limit = f"{error.limit_seconds}-second" if error.limit_seconds else "configured"
        return ErrorSummary(
            code=RUNTIME_LIMIT_CODE,
            summary=_bounded(
                f"The agent stopped at its {limit} runtime limit before finishing. "
                "Raise Max runtime on the agent." + _START_A_NEW_TASK
            ),
            retryable=False,
        )
    if isinstance(error, RunCostBudgetExceeded):
        return ErrorSummary(
            code=COST_LIMIT_CODE,
            summary=_bounded(
                "The agent reached its cost limit before finishing. Raise Max run "
                "cost on the agent." + _START_A_NEW_TASK
            ),
            retryable=False,
        )
    if isinstance(error, ModelGatewayError):
        return ErrorSummary(
            code=error.code, summary=_bounded(error.message), retryable=error.retryable
        )
    return None


def _bounded(text: str) -> str:
    return text[:1000]
