"""Public synchronous workflow execution entry point."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, RLock

from contracts.v1 import Permission
from modules.workflows.runtime_execution_host import WorkflowExecutionRuntimeHost
from modules.workflows.runtime_result import RuntimeResult
from shared.errors import IdempotencyKeyAlreadyExists
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowExecutionEntryMixin(WorkflowExecutionRuntimeHost):
    """Authorize workflow runs and coordinate idempotent request replay."""

    def run_workflow(
        self,
        workflow_id: str,
        task: str,
        requested_by_principal_id: str = "local-developer",
        thread_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        pinned_workflow_version_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Run a published ordered workflow with bounded parallel groups."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_RUN)
        self.authorize(actor, Permission.AGENTS_RUN)
        task = task.strip()
        if not task or len(task) > 12_000:
            raise ValueError("task must contain 1 to 12000 characters")
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("Idempotency-Key must contain 1 to 128 characters")

        return self._execute_workflow_request(
            workflow_id,
            task,
            actor,
            thread_id,
            idempotency_key,
            pinned_workflow_version_id,
            _cancellation_event,
            _cancellation_check,
        )

    def _execute_workflow_request(
        self,
        workflow_id: str,
        task: str,
        actor: str,
        thread_id: str | None,
        idempotency_key: str | None,
        version_id: str | None,
        cancellation_event: Event | None,
        cancellation_check: Callable[[], bool] | None,
    ) -> RuntimeResult:
        """Execute directly or under the scoped idempotency lock."""
        if idempotency_key is None:
            return self._execute_workflow_sequence(
                workflow_id,
                task,
                actor,
                thread_id,
                None,
                pinned_workflow_version_id=version_id,
                cancellation_event=cancellation_event,
                cancellation_check=cancellation_check,
            )
        lock_key = (actor, idempotency_key)
        with self._idempotency_guard:
            lock = self._idempotency_locks.setdefault(lock_key, RLock())
        with lock:
            replayed = self._find_replayed_workflow_run(
                idempotency_key, actor, task, thread_id, workflow_id
            )
            if replayed is not None:
                return replayed
            try:
                return self._execute_workflow_sequence(
                    workflow_id,
                    task,
                    actor,
                    thread_id,
                    idempotency_key,
                    pinned_workflow_version_id=version_id,
                    cancellation_event=cancellation_event,
                    cancellation_check=cancellation_check,
                )
            except IdempotencyKeyAlreadyExists:
                replayed = self._find_replayed_workflow_run(
                    idempotency_key, actor, task, thread_id, workflow_id
                )
                if replayed is None:
                    raise
                return replayed

    def _find_replayed_workflow_run(
        self,
        key: str,
        actor: str,
        task: str,
        thread_id: str | None,
        workflow_id: str,
    ) -> RuntimeResult | None:
        """Return the prior scoped workflow result when its key exists."""
        run = self.runs.find_by_idempotency_key(
            key, actor, effective_org_id(), effective_workspace_id()
        )
        if run is None:
            return None
        return self._replay_idempotent_run(
            run, task=task, agent_id=None, thread_id=thread_id, workflow_id=workflow_id
        )
