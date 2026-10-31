"""Workspace knowledge document use cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from application.ports.repository import Repository
from contracts.v1 import AuditEvent, AuditTargetScope, KnowledgeDocument, Permission
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class KnowledgeService:
    """Manage workspace documents using explicit persistence and auth ports."""

    def __init__(
        self,
        *,
        documents: Repository[KnowledgeDocument],
        audit_events: Repository[AuditEvent],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
    ) -> None:
        """Bind the repositories and request-independent runtime operations."""
        self._documents = documents
        self._audit_events = audit_events
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize

    def list_documents(self) -> tuple[KnowledgeDocument, ...]:
        """List documents visible in the active organization and workspace."""
        self._ensure_bootstrapped()
        self._authorize("local-developer", Permission.PLUGINS_MANAGE)
        return self._documents.list_scoped(effective_org_id(), effective_workspace_id())

    def create_document(self, *, title: str, text: str) -> KnowledgeDocument:
        """Create workspace context and record its audit event."""
        self._ensure_bootstrapped()
        actor = effective_principal_id("local-developer")
        self._authorize(actor, Permission.PLUGINS_MANAGE)
        timestamp = _now()
        document_id = _id()
        document = KnowledgeDocument(
            document_id=document_id,
            source_id=f"knowledge:{document_id}",
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            title=title,
            text=text,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._documents.create(document)
        self._record_audit(document, actor, "knowledge_document.created", timestamp)
        return document

    def delete_document(self, document_id: str) -> None:
        """Remove workspace context and record the deletion in the audit log."""
        self._ensure_bootstrapped()
        actor = effective_principal_id("local-developer")
        self._authorize(actor, Permission.PLUGINS_MANAGE)
        document = self._documents.get_scoped(
            document_id, effective_org_id(), effective_workspace_id()
        )
        if document.is_seeded:
            raise ValueError("starter knowledge documents cannot be deleted")
        self._documents.delete(document_id)
        self._record_audit(document, actor, "knowledge_document.deleted", _now())

    def _ensure_bootstrapped(self) -> None:
        """Initialize local defaults before the first capability operation."""
        if not self._is_bootstrapped():
            self._bootstrap()

    def _record_audit(
        self,
        document: KnowledgeDocument,
        actor: str,
        action: str,
        timestamp: datetime,
    ) -> None:
        """Persist a workspace-scoped event for a knowledge change."""
        self._audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=document.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=document.workspace_id,
                actor_principal_id=actor,
                action=action,
                target_type="knowledge_document",
                target_id=document.document_id,
                summary=(
                    "Knowledge document added"
                    if action.endswith("created")
                    else "Knowledge document removed"
                ),
                request_id=document.document_id,
                metadata={},
                created_at=timestamp,
            )
        )
