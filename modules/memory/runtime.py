"""Conversation-thread and scoped memory use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AuditEvent,
    AuditTargetScope,
    ConversationThread,
    ConversationThreadStatus,
    MemoryItem,
    MemoryRetentionClass,
    MemoryScope,
    Permission,
    WorkflowDefinition,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class MemoryService:
    """Manage conversation threads and scoped memory through explicit ports."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        audit_events: Repository[AuditEvent],
        memory_items: Repository[MemoryItem],
        threads: Repository[ConversationThread],
        workflows: Repository[WorkflowDefinition],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        authorize_organization: Callable[[str, Permission], None],
        default_agent_id: Callable[[], str],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind the persistence ports and platform policies for memory use cases."""
        self.agents = agents
        self.audit_events = audit_events
        self.memory_items = memory_items
        self.threads = threads
        self.workflows = workflows
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._authorize_organization = authorize_organization
        self._default_agent_id = default_agent_id
        self._open_unit_of_work = unit_of_work

    def _ensure_bootstrapped(self) -> None:
        """Initialize built-in records before memory use cases run."""
        if not self._is_bootstrapped():
            self._bootstrap()

    def create_conversation_thread(
        self,
        *,
        agent_id: str | None = None,
        title: str = "New conversation",
        requested_by_principal_id: str = "local-developer",
    ) -> ConversationThread:
        """Open a thread bound to an active agent in the current workspace."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize(requested_by_principal_id, Permission.AGENTS_RUN)
        selected_agent_id = agent_id or self._default_agent_id()
        agent = self.agents.get_scoped(
            selected_agent_id, effective_org_id(), effective_workspace_id()
        )
        if agent.status is not AgentStatus.ACTIVE:
            raise ValueError("thread agent must be active")
        now = _now()
        thread = ConversationThread(
            thread_id=_id(),
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            agent_id=agent.id,
            title=title,
            status=ConversationThreadStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
        self.threads.create(thread)
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=thread.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=thread.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="conversation_thread.created",
                target_type="conversation_thread",
                target_id=thread.thread_id,
                summary="Agent conversation created",
                request_id=thread.thread_id,
                metadata={"agent_id": thread.agent_id},
                created_at=now,
            )
        )
        return thread

    def close_conversation_thread(
        self,
        thread_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> ConversationThread:
        """Close an open conversation thread and audit the change."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize(requested_by_principal_id, Permission.AGENTS_RUN)
        thread = self.threads.get_scoped(
            thread_id, effective_org_id(), effective_workspace_id()
        )
        if thread.status is ConversationThreadStatus.CLOSED:
            return thread
        now = _now()
        updated = thread.model_copy(
            update={"status": ConversationThreadStatus.CLOSED, "updated_at": now}
        )
        updated = ConversationThread.model_validate(updated.model_dump())
        self.threads.put(updated)
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=thread.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=thread.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="conversation_thread.closed",
                target_type="conversation_thread",
                target_id=thread_id,
                summary="Agent conversation closed",
                request_id=thread_id,
                metadata={},
                created_at=now,
            )
        )
        return updated

    def list_conversation_threads(self) -> tuple[ConversationThread, ...]:
        """List threads visible to the active principal and workspace."""
        self._ensure_bootstrapped()
        self._authorize(
            effective_principal_id("local-developer"), Permission.AGENTS_RUN
        )
        return self.threads.list_scoped(effective_org_id(), effective_workspace_id())

    def get_conversation_thread(self, thread_id: str) -> ConversationThread:
        """Load one thread after enforcing active workspace scope."""
        self._ensure_bootstrapped()
        self._authorize(
            effective_principal_id("local-developer"), Permission.AGENTS_RUN
        )
        return self.threads.get_scoped(
            thread_id, effective_org_id(), effective_workspace_id()
        )

    def create_memory_item(
        self,
        *,
        title: str,
        text: str,
        scope: MemoryScope = MemoryScope.WORKSPACE,
        retention_class: MemoryRetentionClass = MemoryRetentionClass.STANDARD,
        scope_ref: str | None = None,
        expires_at: datetime | None = None,
        source_ref: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> MemoryItem:
        """Create explicitly managed organization or workspace memory."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        now = _now()
        self._authorize_memory_write(requested_by_principal_id, scope)
        self._validate_memory_scope(scope, retention_class, scope_ref, expires_at, now)
        item = MemoryItem(
            memory_id=_id(),
            org_id=effective_org_id(),
            workspace_id=(
                None if scope is MemoryScope.ORGANIZATION else effective_workspace_id()
            ),
            scope=scope,
            scope_ref=scope_ref,
            retention_class=retention_class,
            title=title,
            text=text,
            source_ref=source_ref,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        with self._open_unit_of_work():
            self.memory_items.create(item)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=item.org_id,
                    target_scope=(
                        AuditTargetScope.WORKSPACE
                        if item.workspace_id
                        else AuditTargetScope.ORGANIZATION
                    ),
                    workspace_id=item.workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="memory_item.created",
                    target_type="memory_item",
                    target_id=item.memory_id,
                    summary="Memory item added",
                    request_id=item.memory_id,
                    metadata={
                        "scope": scope.value,
                        **({"scope_ref": scope_ref} if scope_ref else {}),
                    },
                    created_at=now,
                )
            )
        return item

    def _authorize_memory_write(self, actor: str, scope: MemoryScope) -> None:
        if scope is MemoryScope.ORGANIZATION:
            self._authorize_organization(actor, Permission.PLUGINS_MANAGE)
            return
        self._authorize(actor, Permission.PLUGINS_MANAGE)

    def _validate_memory_scope(
        self,
        scope: MemoryScope,
        retention_class: MemoryRetentionClass,
        scope_ref: str | None,
        expires_at: datetime | None,
        now: datetime,
    ) -> None:
        if retention_class is MemoryRetentionClass.EPHEMERAL and expires_at is None:
            raise ValueError("ephemeral memory requires expires_at")
        self._validate_memory_scope_reference(scope, scope_ref)
        if expires_at is not None and expires_at <= now:
            raise ValueError("expires_at must be in the future")

    def _validate_memory_scope_reference(
        self, scope: MemoryScope, scope_ref: str | None
    ) -> None:
        if scope in {MemoryScope.THREAD, MemoryScope.WORKFLOW}:
            self._validate_thread_or_workflow_reference(scope, scope_ref)
            return
        if scope in {MemoryScope.WORKSPACE, MemoryScope.ORGANIZATION}:
            if scope_ref:
                raise ValueError(f"{scope.value} memory cannot specify scope_ref")
            return
        raise ValueError("memory records cannot use the none scope")

    def _validate_thread_or_workflow_reference(
        self, scope: MemoryScope, scope_ref: str | None
    ) -> None:
        if not scope_ref:
            raise ValueError("thread/workflow memory requires scope_ref")
        try:
            repository = self.threads if scope is MemoryScope.THREAD else self.workflows
            repository.get_scoped(
                scope_ref, effective_org_id(), effective_workspace_id()
            )
        except KeyError as exc:
            raise ValueError("memory scope_ref was not found") from exc

    def list_memory_items(
        self, requested_by_principal_id: str = "local-developer"
    ) -> tuple[MemoryItem, ...]:
        """List shared organization memory and memory in the selected workspace."""
        if not self._is_bootstrapped():
            self._bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self._authorize(actor, Permission.PLUGINS_MANAGE)
        timestamp = _now()
        return tuple(
            item
            for item in self.memory_items.list_scoped(effective_org_id())
            if item.workspace_id in {None, effective_workspace_id()}
            and (item.expires_at is None or item.expires_at > timestamp)
        )

    def purge_expired_memory_items(
        self, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        """Delete expired memory and record each expiry in the audit trail."""
        if not self._is_bootstrapped():
            self._bootstrap()
        timestamp = now or _now()
        expired_ids: list[str] = []
        for candidate in self.memory_items.list():
            if candidate.expires_at is None or candidate.expires_at > timestamp:
                continue
            try:
                current = self.memory_items.get(candidate.memory_id)
            except KeyError:
                continue
            if current.expires_at is None or current.expires_at > timestamp:
                continue
            with self._open_unit_of_work():
                # Re-read inside the unit of work to avoid deleting a record
                # that another operation replaced while the purge was waiting.
                try:
                    current = self.memory_items.get(candidate.memory_id)
                except KeyError:
                    continue
                if current.expires_at is None or current.expires_at > timestamp:
                    continue
                try:
                    self.memory_items.delete(current.memory_id)
                except KeyError:
                    # A different worker may have purged this item first.
                    continue
                self.audit_events.create(
                    AuditEvent(
                        audit_event_id=_id(),
                        org_id=current.org_id,
                        target_scope=(
                            AuditTargetScope.WORKSPACE
                            if current.workspace_id
                            else AuditTargetScope.ORGANIZATION
                        ),
                        workspace_id=current.workspace_id,
                        actor_principal_id="system:memory-retention",
                        action="memory_item.expired",
                        target_type="memory_item",
                        target_id=current.memory_id,
                        summary="Expired memory item removed by retention policy",
                        request_id=current.memory_id,
                        metadata={
                            "scope": current.scope.value,
                            "retention_class": current.retention_class.value,
                        },
                        created_at=timestamp,
                    )
                )
            expired_ids.append(current.memory_id)
        return tuple(expired_ids)

    def delete_memory_item(
        self, memory_id: str, *, requested_by_principal_id: str = "local-developer"
    ) -> None:
        """Delete visible memory and retain a scoped audit record."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        item = self.memory_items.get_scoped(memory_id, effective_org_id())
        if item.workspace_id is None:
            self._authorize_organization(
                requested_by_principal_id, Permission.PLUGINS_MANAGE
            )
        elif item.workspace_id != effective_workspace_id():
            raise KeyError("memory item not found")
        now = _now()
        with self._open_unit_of_work():
            self.memory_items.delete(memory_id)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=item.org_id,
                    target_scope=(
                        AuditTargetScope.WORKSPACE
                        if item.workspace_id
                        else AuditTargetScope.ORGANIZATION
                    ),
                    workspace_id=item.workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="memory_item.deleted",
                    target_type="memory_item",
                    target_id=memory_id,
                    summary="Memory item removed",
                    request_id=memory_id,
                    metadata={
                        "scope": item.scope.value,
                        **({"scope_ref": item.scope_ref} if item.scope_ref else {}),
                    },
                    created_at=now,
                )
            )
