"""Evaluation SuiteRuntime runtime operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from contracts.v1 import (
    AgentStatus,
    AuditEvent,
    AuditTargetScope,
    EvaluationSuite,
    EvaluationSuiteRecord,
    Permission,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)

if TYPE_CHECKING:
    pass

from modules.evaluations.runtime_host import EvaluationRuntimeHost


class EvaluationSuiteRuntimeMixin(EvaluationRuntimeHost):
    """Provide one cohesive set of evaluation runtime operations."""

    def create_evaluation_suite(
        self,
        definition: EvaluationSuite,
        requested_by_principal_id: str = "local-developer",
    ) -> EvaluationSuiteRecord:
        """Save a deterministic evaluation definition in the active workspace."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        self.authorize(actor, Permission.AGENTS_MANAGE)

        agent = self.agents.get_scoped(
            definition.agent_id, effective_org_id(), effective_workspace_id()
        )

        if agent.status is not AgentStatus.ACTIVE:
            raise ValueError("evaluation suites require an active agent")

        timestamp = _now()

        record = EvaluationSuiteRecord(
            suite_id=definition.suite_id,
            definition=definition,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            created_by_principal_id=actor,
            updated_by_principal_id=actor,
            created_at=timestamp,
            updated_at=timestamp,
        )

        with self._unit_of_work():
            self.evaluation_suites.create(record)

            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=record.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=record.workspace_id,
                    actor_principal_id=actor,
                    action="evaluation.suite.created",
                    target_type="evaluation_suite",
                    target_id=record.suite_id,
                    summary="Evaluation suite created",
                    request_id=record.suite_id,
                    metadata={"agent_id": definition.agent_id},
                    created_at=timestamp,
                )
            )

        return record

    def list_evaluation_suites(self) -> tuple[EvaluationSuiteRecord, ...]:
        """List suites in the active workspace, most recently updated first."""
        if not self._is_bootstrapped():
            self.bootstrap()

        self.authorize(
            effective_principal_id("local-developer"), Permission.AGENTS_MANAGE
        )

        records = self.evaluation_suites.list_scoped(
            effective_org_id(), effective_workspace_id()
        )

        return tuple(sorted(records, key=lambda item: item.updated_at, reverse=True))

    def get_evaluation_suite(
        self,
        suite_id: str,
        requested_by_principal_id: str = "local-developer",
        *,
        manage: bool = False,
    ) -> EvaluationSuiteRecord:
        """Load a suite after checking run or management permissions."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        self.authorize(
            actor,
            Permission.AGENTS_MANAGE if manage else Permission.AGENTS_RUN,
        )

        return self.evaluation_suites.get_scoped(
            suite_id, effective_org_id(), effective_workspace_id()
        )

    def update_evaluation_suite(
        self,
        suite_id: str,
        definition: EvaluationSuite,
        requested_by_principal_id: str = "local-developer",
    ) -> EvaluationSuiteRecord:
        """Replace an existing suite definition without changing its ID."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        self.authorize(actor, Permission.AGENTS_MANAGE)

        if definition.suite_id != suite_id:
            raise ValueError("suite ID cannot be changed")

        agent = self.agents.get_scoped(
            definition.agent_id, effective_org_id(), effective_workspace_id()
        )

        if agent.status is not AgentStatus.ACTIVE:
            raise ValueError("evaluation suites require an active agent")

        current = self.evaluation_suites.get_scoped(
            suite_id, effective_org_id(), effective_workspace_id()
        )

        timestamp = _now()

        updated = EvaluationSuiteRecord.model_validate(
            current.model_copy(
                update={
                    "definition": definition,
                    "updated_by_principal_id": actor,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )

        with self._unit_of_work():
            self.evaluation_suites.put(updated)

            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=updated.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=updated.workspace_id,
                    actor_principal_id=actor,
                    action="evaluation.suite.updated",
                    target_type="evaluation_suite",
                    target_id=updated.suite_id,
                    summary="Evaluation suite updated",
                    request_id=updated.suite_id,
                    metadata={"agent_id": definition.agent_id},
                    created_at=timestamp,
                )
            )

        return updated
