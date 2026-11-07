"""Workspace-scoped read operations for tasks and their transcripts."""

from collections.abc import Callable
from datetime import datetime

from application.ports.repository import Repository
from contracts.v1 import Task, TaskMessage, TaskStatus
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class TaskQueryService:
    """Read task pages and transcript messages through scoped repositories."""

    def __init__(
        self,
        *,
        tasks: Repository[Task],
        messages: Repository[TaskMessage],
        authorize_view: Callable[[], None],
    ) -> None:
        """Bind task storage and the workspace visibility policy."""
        self.tasks = tasks
        self.messages = messages
        self._authorize_view = authorize_view

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        agent_id: str | None = None,
        created_by_principal_id: str | None = None,
        limit: int = 50,
        cursor_created_at: datetime | None = None,
        cursor_task_id: str | None = None,
    ) -> tuple[Task, ...]:
        """Return newest tasks visible in the active workspace."""
        self._authorize_view()
        principal_id = effective_principal_id("local-developer")
        return self.tasks.list_task_page(
            effective_org_id(),
            effective_workspace_id(),
            status=status.value if status else None,
            agent_id=agent_id,
            created_by_principal_id=created_by_principal_id,
            visible_to_principal_id=principal_id,
            cursor_created_at=cursor_created_at,
            cursor_task_id=cursor_task_id,
            limit=limit,
        )

    def get_task(self, task_id: str) -> Task:
        """Return a task scoped to the authenticated workspace."""
        self._authorize_view()
        task = self.tasks.get_scoped(
            task_id, effective_org_id(), effective_workspace_id()
        )
        principal_id = effective_principal_id("local-developer")
        if (
            task.audience.value == "private"
            and task.audience_owner_principal_id != principal_id
        ):
            raise KeyError("task not found")
        return task

    def list_messages(
        self,
        task_id: str,
        *,
        limit: int = 100,
        cursor_created_at: datetime | None = None,
        cursor_message_id: str | None = None,
    ) -> tuple[TaskMessage, ...]:
        """Return a task transcript in chronological order."""
        task = self.get_task(task_id)
        page = self.messages.list_task_messages_page(
            task.task_id,
            task.org_id,
            task.workspace_id,
            cursor_created_at=cursor_created_at,
            cursor_message_id=cursor_message_id,
            limit=limit,
        )
        return tuple(reversed(page))
