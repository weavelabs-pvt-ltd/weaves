"""Atomic persistence and replay for newly created task aggregates."""

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import ConversationThread, Task, TaskMessage, TaskTurn
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import effective_org_id, effective_workspace_id


class TaskCreationPersistence:
    """Persist a task, thread, first message, and queued turn as one unit."""

    def __init__(
        self,
        *,
        tasks: Repository[Task],
        threads: Repository[ConversationThread],
        messages: Repository[TaskMessage],
        turns: Repository[TaskTurn],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind the task aggregate repositories and transaction port."""
        self.tasks = tasks
        self.threads = threads
        self.messages = messages
        self.turns = turns
        self._open_unit_of_work = unit_of_work

    def find_existing(
        self,
        key: str | None,
        principal: str,
        agent_id: str,
        title: str,
        description: str,
        initial_message: str,
    ) -> Task | None:
        """Return an idempotent replay or reject a conflicting request."""
        if not key:
            return None
        existing = self.tasks.find_by_idempotency_key(
            key, principal, effective_org_id(), effective_workspace_id()
        )
        if existing is None:
            return None
        return self._validate_replay(
            existing, agent_id, title, description, initial_message
        )

    def persist(
        self,
        task: Task,
        thread: ConversationThread,
        message: TaskMessage,
        turn: TaskTurn,
        principal: str,
        agent_id: str,
        title: str,
        description: str,
        initial_message: str,
    ) -> Task:
        """Persist the aggregate and resolve a concurrent idempotent winner."""
        try:
            with self._open_unit_of_work():
                self.tasks.create(task)
                self.threads.create(thread)
                self.messages.create(message)
                self.turns.create(turn)
            return task
        except IdempotencyKeyAlreadyExists as exc:
            existing = self.tasks.find_by_idempotency_key(
                task.idempotency_key or "",
                principal,
                effective_org_id(),
                effective_workspace_id(),
            )
            if existing is None:
                raise
            try:
                return self._validate_replay(
                    existing, agent_id, title, description, initial_message
                )
            except IdempotencyConflict as conflict:
                raise conflict from exc

    def _validate_replay(
        self,
        task: Task,
        agent_id: str,
        title: str,
        description: str,
        initial_message: str,
    ) -> Task:
        """Reject reusing an idempotency key for different task data."""
        original = self.messages.get_scoped(
            task.creation_message_id, task.org_id, task.workspace_id
        )
        if (
            task.agent_id != agent_id
            or task.title != title
            or task.description != description
            or original.content != initial_message.strip()
        ):
            raise IdempotencyConflict(
                "Idempotency-Key was already used for a different task"
            )
        return task
