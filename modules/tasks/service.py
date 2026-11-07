"""Task conversation lifecycle and durable turn submission use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    AgentSkillSnapshot,
    AgentStatus,
    AgentVersion,
    ConversationThread,
    Permission,
    PluginInstallation,
    SkillDefinition,
    SkillVersion,
    Task,
    TaskAudience,
    TaskMessage,
    TaskMessageRole,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
)
from modules.tasks.creation_persistence import TaskCreationPersistence
from modules.tasks.execution_summaries import TaskExecutionSummaries
from modules.tasks.lifecycle import TaskLifecycleMixin
from modules.tasks.private_skill_resolver import PrivateTaskSkillResolver
from modules.tasks.query_service import TaskQueryService
from shared.clock import new_id, utc_now
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class TaskService(TaskLifecycleMixin):
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
        skill_definitions: Repository[SkillDefinition],
        skill_versions: Repository[SkillVersion],
        capability_installation: Callable[..., tuple[PluginInstallation, object]],
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
        self.skill_definitions = skill_definitions
        self.skill_versions = skill_versions
        self._capability_installation = capability_installation
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work
        self.queries = TaskQueryService(
            tasks=tasks, messages=messages, authorize_view=self._authorize_view
        )
        self.private_skill_resolver = PrivateTaskSkillResolver(
            definitions=skill_definitions,
            versions=skill_versions,
            authorize=authorize,
            capability_installation=capability_installation,
        )
        self.execution_summary_builder = TaskExecutionSummaries(
            turns=turns, agent_runs=agent_runs, authorize_view=self._authorize_view
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
        audience: TaskAudience = TaskAudience.WORKSPACE,
        skill_version_ids: tuple[str, ...] = (),
    ) -> Task:
        """Create a task, thread, first user message, and queued agent turn."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize_task_creation(principal, workflow_id, initial_message)
        existing = self._find_existing_task(
            idempotency_key,
            principal,
            agent_id,
            title,
            description,
            initial_message,
            audience,
            skill_version_ids,
        )
        if existing is not None:
            return existing
        version, snapshots = self._resolve_task_version(
            agent_id,
            principal,
            workflow_id,
            pinned_version_id,
            condition_only,
            audience,
            skill_version_ids,
        )
        return self._persist_task_start(
            agent_id,
            title,
            description,
            initial_message,
            principal,
            version,
            snapshots,
            audience,
            idempotency_key,
        )

    def _authorize_task_creation(
        self, principal: str, workflow_id: str | None, initial_message: str
    ) -> None:
        """Authorize task creation and validate its initial user message."""
        permission = (
            Permission.WORKFLOWS_RUN if workflow_id else Permission.TASKS_CREATE
        )
        self._authorize(principal, permission)
        self._authorize(principal, Permission.AGENTS_RUN)
        self._validate_message(initial_message)

    def _find_existing_task(
        self,
        key: str | None,
        principal: str,
        agent_id: str,
        title: str,
        description: str,
        message: str,
        audience: TaskAudience,
        skill_version_ids: tuple[str, ...],
    ) -> Task | None:
        """Return a prior idempotent creation when its request matches."""
        return self.creation.find_existing(
            key,
            principal,
            agent_id,
            title,
            description,
            message,
            private=audience is TaskAudience.PRIVATE,
            skill_version_ids=skill_version_ids,
        )

    def _resolve_task_version(
        self,
        agent_id: str,
        principal: str,
        workflow_id: str | None,
        pinned_version_id: str | None,
        condition_only: bool,
        audience: TaskAudience,
        skill_version_ids: tuple[str, ...],
    ) -> tuple[AgentVersion, tuple[AgentSkillSnapshot, ...]]:
        """Validate the chosen agent version and pin its task skill context."""
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
        snapshots = self.private_skill_resolver.resolve(
            skill_version_ids,
            version.allowed_capability_ids,
            owner=principal,
            audience=audience,
            workflow_id=workflow_id,
            condition_only=condition_only,
        )
        self._validate_combined_skill_context(version, snapshots)
        if condition_only:
            version = self._condition_version(version)
            self.agent_versions.create(version)
        return version, snapshots

    @staticmethod
    def _validate_combined_skill_context(
        version: AgentVersion, snapshots: tuple[AgentSkillSnapshot, ...]
    ) -> None:
        """Enforce count and byte limits across organization and private skills."""
        skills = (*version.skills, *snapshots)
        if len(skills) > 5:
            raise ValueError("a task may use at most five combined skills")
        size = sum(
            len(skill.instructions.encode("utf-8"))
            + sum(len(text.encode("utf-8")) for text in skill.files.values())
            for skill in skills
        )
        if size > 131_072:
            raise ValueError("combined task skills exceed the 128 KiB context limit")

    @staticmethod
    def _condition_version(version: AgentVersion) -> AgentVersion:
        """Create a tool-free, skill-free version for condition-only evaluation."""
        return AgentVersion.model_validate(
            version.model_copy(
                update={
                    "agent_version_id": new_id(),
                    "allowed_capability_ids": (),
                    "skills": (),
                    "instructions": (
                        "Evaluate the condition in the request against its supplied "
                        "results. Respond only with the JSON boolean true or false. "
                        "Do not invoke tools or perform actions."
                    ),
                    "output_schema": {"type": "boolean"},
                }
            ).model_dump()
        )

    def _persist_task_start(
        self,
        agent_id: str,
        title: str,
        description: str,
        initial_message: str,
        principal: str,
        version: AgentVersion,
        snapshots: tuple[AgentSkillSnapshot, ...],
        audience: TaskAudience,
        idempotency_key: str | None,
    ) -> Task:
        """Persist the task aggregate and its first queued conversation turn."""
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
            audience=audience,
            audience_owner_principal_id=(
                principal if audience is TaskAudience.PRIVATE else None
            ),
            private_skill_snapshots=snapshots,
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
            private=audience is TaskAudience.PRIVATE,
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

    def execution_summaries(
        self, tasks: tuple[Task, ...]
    ) -> dict[str, dict[str, object]]:
        """Build execution metadata for visible tasks."""
        return self.execution_summary_builder.build(tasks)

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
