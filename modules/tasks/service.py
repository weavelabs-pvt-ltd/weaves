"""Task conversation lifecycle and durable turn submission use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    AgentStatus,
    AgentVersion,
    ConversationThread,
    Permission,
    Task,
    TaskMessage,
    TaskMessageRole,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
)
from modules.tasks.creation_persistence import TaskCreationPersistence
from modules.tasks.query_service import TaskQueryService
from shared.clock import new_id, utc_now
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class TaskService:
    """Create and manage workspace task conversations and their queued turns."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_versions: Repository[AgentVersion],
        threads: Repository[ConversationThread],
        tasks: Repository[Task],
        messages: Repository[TaskMessage],
        turns: Repository[TaskTurn],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        agent_runs: Repository[AgentRun] | None = None,
    ) -> None:
        """Bind task persistence, identity enforcement, and transaction ports."""
        self.agent_runs = agent_runs
        self.agents = agents
        self.agent_versions = agent_versions
        self.threads = threads
        self.tasks = tasks
        self.messages = messages
        self.turns = turns
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work
        self.queries = TaskQueryService(
            tasks=tasks, messages=messages, authorize_view=self._authorize_view
        )
        self.creation = TaskCreationPersistence(
            tasks=tasks,
            threads=threads,
            messages=messages,
            turns=turns,
            unit_of_work=unit_of_work,
        )

    def create_task(
        self,
        *,
        agent_id: str,
        title: str,
        initial_message: str,
        description: str = "",
        idempotency_key: str | None = None,
        workflow_id: str | None = None,
        pinned_version_id: str | None = None,
        condition_only: bool = False,
    ) -> Task:
        """Create a task, thread, first user message, and queued agent turn."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize(
            principal,
            Permission.WORKFLOWS_RUN if workflow_id else Permission.TASKS_CREATE,
        )
        self._authorize(principal, Permission.AGENTS_RUN)
        self._validate_message(initial_message)
        existing = self.creation.find_existing(
            idempotency_key, principal, agent_id, title, description, initial_message
        )
        if existing is not None:
            return existing
        agent = self.agents.get_scoped(
            agent_id, effective_org_id(), effective_workspace_id()
        )
        if agent.status is not AgentStatus.ACTIVE or not agent.current_version_id:
            raise ValueError("tasks require an active agent with a published version")
        version = self.agent_versions.get_scoped(
            pinned_version_id or agent.current_version_id,
            effective_org_id(),
            effective_workspace_id(),
        )
        if version.agent_id != agent_id or (
            workflow_id and workflow_id not in version.allowed_workflow_ids
        ):
            raise ValueError("This agent version does not permit the workflow")
        if condition_only:
            version = AgentVersion.model_validate(
                version.model_copy(
                    update={
                        "agent_version_id": new_id(),
                        "allowed_capability_ids": (),
                        "skills": (),
                        "instructions": (
                            "Evaluate the condition in the request against "
                            "its supplied results. Respond only with the JSON "
                            "boolean true or false. Do not invoke tools or "
                            "perform actions."
                        ),
                        "output_schema": {"type": "boolean"},
                    }
                ).model_dump()
            )
            self.agent_versions.create(version)
        now = utc_now()
        task_id, thread_id, turn_id, message_id = (new_id() for _ in range(4))
        task = Task(
            task_id=task_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            title=title,
            description=description,
            created_by_principal_id=principal,
            requested_by_principal_id=principal,
            agent_id=agent_id,
            agent_version_id=version.agent_version_id,
            thread_id=thread_id,
            creation_message_id=message_id,
            status=TaskStatus.OPEN,
            active_turn_id=turn_id,
            active_turn_status=TaskTurnStatus.QUEUED,
            latest_turn_id=turn_id,
            latest_turn_status=TaskTurnStatus.QUEUED,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        thread = ConversationThread(
            thread_id=thread_id,
            org_id=task.org_id,
            workspace_id=task.workspace_id,
            agent_id=agent_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        message = self._user_message(task, turn_id, message_id, initial_message, now)
        turn = self._queued_turn(task, turn_id, message_id, principal, now)
        return self.creation.persist(
            task,
            thread,
            message,
            turn,
            principal,
            agent_id,
            title,
            description,
            initial_message,
        )

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
        """Return the newest visible tasks in the current workspace."""
        return self.queries.list_tasks(
            status=status,
            agent_id=agent_id,
            created_by_principal_id=created_by_principal_id,
            cursor_created_at=cursor_created_at,
            cursor_task_id=cursor_task_id,
            limit=limit,
        )

    def get_task(self, task_id: str) -> Task:
        """Return a task scoped to the authenticated workspace."""
        return self.queries.get_task(task_id)

    def list_messages(
        self,
        task_id: str,
        *,
        limit: int = 100,
        cursor_created_at: datetime | None = None,
        cursor_message_id: str | None = None,
    ) -> tuple[TaskMessage, ...]:
        """Return a task transcript in chronological order."""
        return self.queries.list_messages(
            task_id,
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_message_id=cursor_message_id,
        )

    def add_message(
        self, task_id: str, content: str, *, idempotency_key: str | None = None
    ) -> TaskTurn:
        """Append one user message and atomically enqueue its agent response."""
        self._ensure_ready()
        self._validate_message(content)
        principal = effective_principal_id("local-developer")
        self._authorize(principal, Permission.TASKS_CREATE)
        self._authorize(principal, Permission.AGENTS_RUN)
        task = self.get_task(task_id)
        if idempotency_key:
            existing = self.turns.find_by_idempotency_key(
                idempotency_key, principal, task.org_id, task.workspace_id
            )
            if existing is not None:
                original = self.messages.get_scoped(
                    existing.user_message_id, task.org_id, task.workspace_id
                )
                if original.content != content.strip():
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used for a different message"
                    )
                return existing
        if task.status is not TaskStatus.OPEN:
            raise ValueError("messages can only be added to open tasks")
        if task.active_turn_id:
            raise ValueError("this task already has a turn in progress")
        now, turn_id, message_id = utc_now(), new_id(), new_id()
        message = self._user_message(task, turn_id, message_id, content, now)
        turn = self._queued_turn(
            task, turn_id, message_id, principal, now, idempotency_key
        )
        updated = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_id": turn_id,
                    "active_turn_status": TaskTurnStatus.QUEUED,
                    "latest_turn_id": turn_id,
                    "latest_turn_status": TaskTurnStatus.QUEUED,
                    "updated_at": now,
                }
            ).model_dump()
        )
        try:
            with self._open_unit_of_work():
                self.messages.create(message)
                self.turns.create(turn)
                changed = self.tasks.put_if_task_state(
                    updated,
                    expected_status=TaskStatus.OPEN.value,
                    expected_active_turn_id=None,
                )
                if not changed:
                    self.messages.delete(message_id)
                    self.turns.delete(turn_id)
                    raise ValueError("this task already has a turn in progress")
        except IdempotencyKeyAlreadyExists as exc:
            existing = self.turns.find_by_idempotency_key(
                idempotency_key or "", principal, task.org_id, task.workspace_id
            )
            if existing is not None:
                original = self.messages.get_scoped(
                    existing.user_message_id, task.org_id, task.workspace_id
                )
                if original.content == content.strip():
                    return existing
                raise IdempotencyConflict(
                    "Idempotency-Key was already used for a different message"
                ) from exc
            raise
        return turn

    def cancel_turn(self, task_id: str, turn_id: str) -> None:
        """Cancel a workflow child task and revoke its active worker lease."""
        self._authorize(
            effective_principal_id("local-developer"), Permission.WORKFLOWS_RUN
        )
        with self._open_unit_of_work():
            task = self.tasks.get_scoped(
                task_id, effective_org_id(), effective_workspace_id()
            )
            turn = self.turns.get_scoped(turn_id, task.org_id, task.workspace_id)
            if turn.task_id != task_id or task.active_turn_id != turn_id:
                return
            now = utc_now()
            cancelled = TaskTurn.model_validate(
                turn.model_copy(
                    update={
                        "status": TaskTurnStatus.CANCELLED,
                        "worker_id": None,
                        "finished_at": now,
                        "updated_at": now,
                        "error": None,
                    }
                ).model_dump()
            )
            self.turns.put(cancelled)
            updated = Task.model_validate(
                task.model_copy(
                    update={
                        "active_turn_id": None,
                        "active_turn_status": None,
                        "latest_turn_status": TaskTurnStatus.CANCELLED,
                        "updated_at": now,
                    }
                ).model_dump()
            )
            self.tasks.put_if_task_state(
                updated,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=turn_id,
            )

    def complete_task(self, task_id: str) -> Task:
        """Mark an idle task completed; its history remains available."""
        return self._change_status(task_id, TaskStatus.COMPLETED)

    def retry_turn(self, task_id: str, turn_id: str) -> TaskTurn:
        """Requeue a failed turn using a fresh agent-run idempotency key."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize(principal, Permission.TASKS_CREATE)
        self._authorize(principal, Permission.AGENTS_RUN)
        task = self.tasks.get_scoped(
            task_id, effective_org_id(), effective_workspace_id()
        )
        turn = self.turns.get_scoped(turn_id, task.org_id, task.workspace_id)
        if turn.task_id != task.task_id:
            raise KeyError(turn_id)
        if turn.status is not TaskTurnStatus.FAILED:
            raise ValueError("only failed task turns can be retried")
        if task.status is not TaskStatus.OPEN or task.active_turn_id is not None:
            raise ValueError("only idle open tasks can retry a turn")
        now = utc_now()
        retry = TaskTurn.model_validate(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.QUEUED,
                    "run_idempotency_key": None,
                    "worker_id": None,
                    "agent_run_id": None,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "updated_at": now,
                }
            ).model_dump()
        )
        updated_task = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_id": turn_id,
                    "active_turn_status": TaskTurnStatus.QUEUED,
                    "latest_turn_id": turn_id,
                    "latest_turn_status": TaskTurnStatus.QUEUED,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            changed = self.tasks.put_if_task_state(
                updated_task,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=None,
            )
            if not changed:
                raise ValueError("only idle open tasks can retry a turn")
            self.turns.put(retry)
        return retry

    def archive_task(self, task_id: str) -> Task:
        """Archive an idle task while retaining its transcript."""
        return self._change_status(task_id, TaskStatus.ARCHIVED)

    def reopen_task(self, task_id: str) -> Task:
        """Reopen a completed task for another conversation turn."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize(principal, Permission.TASKS_MANAGE)
        task = self.tasks.get_scoped(
            task_id, effective_org_id(), effective_workspace_id()
        )
        if task.active_turn_id:
            raise ValueError("tasks with a turn in progress cannot be reopened")
        if task.status is TaskStatus.ARCHIVED:
            raise ValueError("archived tasks are read-only")
        if task.status is not TaskStatus.COMPLETED:
            raise ValueError("only completed tasks can be reopened")
        updated = Task.model_validate(
            task.model_copy(
                update={
                    "status": TaskStatus.OPEN,
                    "completed_at": None,
                    "archived_at": None,
                    "updated_at": utc_now(),
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            changed = self.tasks.put_if_task_state(
                updated,
                expected_status=TaskStatus.COMPLETED.value,
                expected_active_turn_id=None,
            )
            if not changed:
                raise ValueError("task state changed; refresh and try again")
        return updated

    def _change_status(self, task_id: str, status: TaskStatus) -> Task:
        """Apply a terminal user lifecycle state to an idle task."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize(principal, Permission.TASKS_MANAGE)
        task = self.tasks.get_scoped(
            task_id, effective_org_id(), effective_workspace_id()
        )
        if task.active_turn_id:
            raise ValueError("tasks with a turn in progress cannot be closed")
        if task.status is not TaskStatus.OPEN:
            raise ValueError("only open tasks can be completed or archived")
        now = utc_now()
        changes = {"status": status, "updated_at": now}
        changes["completed_at" if status is TaskStatus.COMPLETED else "archived_at"] = (
            now
        )
        updated = Task.model_validate(task.model_copy(update=changes).model_dump())
        with self._open_unit_of_work():
            changed = self.tasks.put_if_task_state(
                updated,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=None,
            )
            if not changed:
                raise ValueError("task state changed; refresh and try again")
        return updated

    def execution_summaries(self, tasks: tuple[Task, ...]) -> dict[str, dict[str, Any]]:
        """Expose metering and sources for visible tasks without raw run inputs."""
        self._authorize_view()
        org, workspace = effective_org_id(), effective_workspace_id()
        if any(task.org_id != org or task.workspace_id != workspace for task in tasks):
            raise PermissionError("Task execution metadata is workspace scoped")
        ids = {task.task_id for task in tasks}
        turns = [
            turn
            for turn in self.turns.list_scoped(org, workspace)
            if turn.task_id in ids
        ]
        run_ids = {turn.agent_run_id for turn in turns if turn.agent_run_id}
        runs = (
            {
                run.agent_run_id: run
                for run in self.agent_runs.list_scoped(org, workspace)
                if run.agent_run_id in run_ids
            }
            if self.agent_runs
            else {}
        )
        result = {}
        for task in tasks:
            history = sorted(
                (turn for turn in turns if turn.task_id == task.task_id),
                key=lambda turn: turn.created_at,
            )
            rows = []
            for turn in history:
                run = runs.get(turn.agent_run_id)
                rows.append(
                    {
                        "turn_id": turn.turn_id,
                        "status": str(turn.status),
                        "created_at": turn.created_at,
                        "started_at": turn.started_at,
                        "finished_at": turn.finished_at,
                        "estimated_cost_usd": str(run.estimated_cost_usd)
                        if run and run.estimated_cost_usd is not None
                        else None,
                    }
                )
            latest = next(
                (turn for turn in history if turn.turn_id == task.latest_turn_id), None
            )
            latest_run = runs.get(latest.agent_run_id) if latest else None
            prices = [
                Decimal(row["estimated_cost_usd"])
                for row in rows
                if row["estimated_cost_usd"] is not None
            ]
            result[task.task_id] = {
                "estimated_cost_usd": str(sum(prices)) if prices else None,
                "started_at": latest.started_at if latest else None,
                "finished_at": latest.finished_at if latest else None,
                "agent_run_id": latest.agent_run_id if latest else None,
                "source_ids": (latest_run.output_summary or {}).get("source_ids", [])
                if latest_run
                else [],
                "turns": rows,
            }
        return result

    def _authorize_view(self) -> None:
        """Require task visibility for the active workspace identity."""
        self._ensure_ready()
        self._authorize(
            effective_principal_id("local-developer"), Permission.TASKS_VIEW
        )

    def _ensure_ready(self) -> None:
        """Ensure built-in records exist before task operations."""
        if not self._is_bootstrapped():
            self._bootstrap()

    @staticmethod
    def _validate_message(content: str) -> None:
        """Reject blank and oversized user messages before persistence."""
        if not content.strip() or len(content) > 12_000:
            raise ValueError("message must contain 1 to 12000 characters")

    @staticmethod
    def _user_message(
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

    @staticmethod
    def _queued_turn(
        task: Task,
        turn_id: str,
        message_id: str,
        principal: str,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> TaskTurn:
        """Build an unclaimed durable task turn."""
        return TaskTurn(
            turn_id=turn_id,
            task_id=task.task_id,
            user_message_id=message_id,
            requested_by_principal_id=principal,
            idempotency_key=idempotency_key,
            status=TaskTurnStatus.QUEUED,
            org_id=task.org_id,
            workspace_id=task.workspace_id,
            created_at=now,
            updated_at=now,
        )
