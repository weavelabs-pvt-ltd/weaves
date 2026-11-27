"""Build the transcript message and the queued turn of a task conversation."""

from __future__ import annotations

from datetime import datetime

from contracts.v1 import (
    Task,
    TaskMessage,
    TaskMessageRole,
    TaskTurn,
    TaskTurnStatus,
    TurnOrigin,
)
from shared.correlation import current_request_id


def user_message(
    task: Task, turn_id: str, message_id: str, content: str, now: datetime
) -> TaskMessage:
    """Build the immutable user-authored transcript record."""
    return TaskMessage(
        message_id=message_id,
        task_id=task.task_id,
        turn_id=turn_id,
        org_id=task.org_id,
        workspace_id=task.workspace_id,
        role=TaskMessageRole.USER,
        content=content.strip(),
        created_by_principal_id=task.requested_by_principal_id,
        created_at=now,
    )


def queued_turn(
    task: Task,
    turn_id: str,
    message_id: str,
    principal: str,
    now: datetime,
    idempotency_key: str | None = None,
    origin: TurnOrigin | None = None,
) -> TaskTurn:
    """Build an unclaimed durable task turn (``origin`` marks a Slack turn)."""
    return TaskTurn(
        turn_id=turn_id,
        task_id=task.task_id,
        user_message_id=message_id,
        requested_by_principal_id=principal,
        idempotency_key=idempotency_key,
        request_id=current_request_id(),
        origin=origin,
        status=TaskTurnStatus.QUEUED,
        org_id=task.org_id,
        workspace_id=task.workspace_id,
        created_at=now,
        updated_at=now,
    )
