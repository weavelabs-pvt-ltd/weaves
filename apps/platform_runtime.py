"""Compatibility façade for capability services and legacy runtime callers."""

from datetime import datetime
from typing import Any

import httpx

from application import runtime_constants as _runtime_constants
from application.runtime_bootstrap import RuntimeBootstrapMixin
from application.runtime_compatibility import RuntimeServiceCompatibilityMixin
from application.runtime_foundation import RuntimeFoundationMixin
from application.runtime_recovery import RuntimeRecoveryCompatibilityMixin
from apps.capability_services import CapabilityServiceSet
from contracts.v1 import (
    ExecutionJob,
    Permission,
    PluginInstallation,
    WorkflowSchedule,
    WorkflowScheduleStatus,
    WorkflowTrigger,
    WorkflowTriggerStatus,
)
from modules.agents.runtime_lifecycle import AgentService
from modules.agents.runtime_state import (
    AgentRuntimeBudgetExceeded as AgentRuntimeBudgetExceeded,
)
from modules.agents.runtime_state import (
    RunCostBudgetExceeded as RunCostBudgetExceeded,
)
from modules.evaluations.service import EvaluationService
from modules.models.runtime import ModelService
from modules.workflows.runtime_result import RuntimeResult as RuntimeResult


class LocalPlatformRuntime(
    RuntimeFoundationMixin,
    RuntimeServiceCompatibilityMixin,
    RuntimeBootstrapMixin,
    RuntimeRecoveryCompatibilityMixin,
):
    """Run the modular local platform path with a mock default model."""

    model_service: ModelService
    agent_service: AgentService
    evaluation_service: EvaluationService

    def attach_capability_services(self, services: CapabilityServiceSet) -> None:
        """Expose composed services through the legacy compatibility façade."""
        self._capability_services = services

    def __getattr__(self, name: str) -> Any:
        """Resolve legacy service attributes through the attached service set."""
        services = self.__dict__.get("_capability_services")
        if services is not None:
            try:
                return getattr(services, name)
            except AttributeError:
                pass
        raise AttributeError(name)

    def authenticate_principal(self, principal_id: str) -> str:
        """Resolve a token subject against the active principal directory."""
        return self.credential_service.authenticate_principal(principal_id)

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Check a principal's workspace permission."""
        self.credential_service.authorize(principal_id, permission)

    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Check a principal's organization-level permission."""
        self.credential_service.authorize_organization(principal_id, permission)

    def create_github_app_installation(
        self,
        *,
        display_name: str,
        github_installation_id: str,
        repositories: tuple[str, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Retain GitHub App compatibility through the connection service."""
        return self.integration_connection_service.create_github_app_installation(
            display_name=display_name,
            github_installation_id=github_installation_id,
            repositories=repositories,
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_mcp_installation(
        self,
        *,
        display_name: str,
        endpoint_url: str,
        read_only_tool_names: tuple[str, ...],
        bearer_token: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Retain MCP installation compatibility through the connection service."""
        return self.integration_connection_service.create_mcp_installation(
            display_name=display_name,
            endpoint_url=endpoint_url,
            read_only_tool_names=read_only_tool_names,
            bearer_token=bearer_token,
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_slack_oauth_installation(
        self,
        *,
        display_name: str,
        token_bundle: str,
        channels: tuple[str, ...],
        include_thread_replies: bool = False,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Retain Slack OAuth installation compatibility."""
        return self.integration_connection_service.create_slack_oauth_installation(
            display_name=display_name,
            token_bundle=token_bundle,
            channels=channels,
            include_thread_replies=include_thread_replies,
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_jira_oauth_installation(
        self,
        *,
        display_name: str,
        token_bundle: str,
        site_url: str,
        cloud_id: str,
        project_keys: tuple[str, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Retain Jira OAuth installation compatibility."""
        return self.integration_connection_service.create_jira_oauth_installation(
            display_name=display_name,
            token_bundle=token_bundle,
            site_url=site_url,
            cloud_id=cloud_id,
            project_keys=project_keys,
            requested_by_principal_id=requested_by_principal_id,
        )

    def enqueue_agent_run(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        trigger_id: str | None = None,
        trigger_event_id: str | None = None,
        schedule_id: str | None = None,
        schedule_occurrence_at: datetime | None = None,
        _idempotency_lock_held: bool = False,
    ) -> tuple[ExecutionJob, bool]:
        """Preserve internal trigger compatibility via the execution service."""
        return self.execution_service.enqueue_agent_run(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            idempotency_key,
            workflow_id=workflow_id,
            trigger_id=trigger_id,
            trigger_event_id=trigger_event_id,
            schedule_id=schedule_id,
            schedule_occurrence_at=schedule_occurrence_at,
            _idempotency_lock_held=_idempotency_lock_held,
        )

    def create_workflow_trigger(
        self,
        workflow_id: str,
        *,
        name: str,
        task_instructions: str,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[WorkflowTrigger, str]:
        """Retain workflow trigger creation compatibility."""
        return self.workflow_automation_service.create_workflow_trigger(
            workflow_id,
            name=name,
            task_instructions=task_instructions,
            requested_by_principal_id=requested_by_principal_id,
        )

    def list_workflow_triggers(self) -> tuple[WorkflowTrigger, ...]:
        """Retain workflow trigger listing compatibility."""
        return self.workflow_automation_service.list_workflow_triggers()

    def update_workflow_trigger(
        self,
        trigger_id: str,
        status: WorkflowTriggerStatus | None = None,
        requested_by_principal_id: str = "local-developer",
        *,
        name: str | None = None,
        task_instructions: str | None = None,
    ) -> WorkflowTrigger:
        """Retain workflow trigger updates compatibility."""
        return self.workflow_automation_service.update_workflow_trigger(
            trigger_id,
            status,
            name=name,
            task_instructions=task_instructions,
            requested_by_principal_id=requested_by_principal_id,
        )

    def rotate_workflow_trigger_secret(
        self,
        trigger_id: str,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[WorkflowTrigger, str]:
        """Retain workflow trigger secret rotation compatibility."""
        return self.workflow_automation_service.rotate_workflow_trigger_secret(
            trigger_id, requested_by_principal_id
        )

    def enqueue_workflow_trigger_event(
        self,
        trigger_id: str,
        token: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> tuple[ExecutionJob, bool]:
        """Retain authenticated workflow event enqueue compatibility."""
        return self.workflow_automation_service.enqueue_workflow_trigger_event(
            trigger_id, token, event_id, payload
        )

    def create_workflow_schedule(
        self,
        workflow_id: str,
        *,
        name: str,
        task_instructions: str,
        interval_seconds: int,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowSchedule:
        """Retain workflow schedule creation compatibility."""
        return self.workflow_automation_service.create_workflow_schedule(
            workflow_id,
            name=name,
            task_instructions=task_instructions,
            interval_seconds=interval_seconds,
            requested_by_principal_id=requested_by_principal_id,
        )

    def list_workflow_schedules(self) -> tuple[WorkflowSchedule, ...]:
        """Retain workflow schedule listing compatibility."""
        return self.workflow_automation_service.list_workflow_schedules()

    def update_workflow_schedule(
        self,
        schedule_id: str,
        status: WorkflowScheduleStatus | None = None,
        requested_by_principal_id: str = "local-developer",
        *,
        name: str | None = None,
        task_instructions: str | None = None,
        interval_seconds: int | None = None,
    ) -> WorkflowSchedule:
        """Retain workflow schedule updates compatibility."""
        return self.workflow_automation_service.update_workflow_schedule(
            schedule_id,
            status,
            name=name,
            task_instructions=task_instructions,
            interval_seconds=interval_seconds,
            requested_by_principal_id=requested_by_principal_id,
        )

    def dispatch_due_workflow_schedules(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> tuple[ExecutionJob, ...]:
        """Retain scheduled job dispatch compatibility for existing callers."""
        return self.workflow_automation_service.dispatch_due_workflow_schedules(
            now=now, limit=limit
        )

    def list_execution_jobs(
        self, requested_by_principal_id: str = "local-developer", limit: int = 50
    ) -> tuple[ExecutionJob, ...]:
        """Delegate job listing for callers not yet migrated to the service."""
        return self.execution_service.list_execution_jobs(
            requested_by_principal_id, limit
        )

    def get_execution_job(
        self, job_id: str, requested_by_principal_id: str = "local-developer"
    ) -> ExecutionJob:
        """Delegate scoped job lookup for compatibility callers."""
        return self.execution_service.get_execution_job(
            job_id, requested_by_principal_id
        )

    def cancel_execution_job(
        self, job_id: str, requested_by_principal_id: str = "local-developer"
    ) -> ExecutionJob:
        """Delegate job cancellation for compatibility callers."""
        return self.execution_service.cancel_execution_job(
            job_id, requested_by_principal_id
        )

    def retry_execution_job(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        requested_by_principal_id: str = "local-developer",
        _idempotency_lock_held: bool = False,
    ) -> tuple[ExecutionJob, bool]:
        """Delegate idempotent retries for compatibility callers."""
        return self.execution_service.retry_execution_job(
            job_id,
            idempotency_key=idempotency_key,
            requested_by_principal_id=requested_by_principal_id,
            _idempotency_lock_held=_idempotency_lock_held,
        )

    def process_next_execution_job(self, worker_id: str) -> ExecutionJob | None:
        """Keep worker compatibility while delegating to its execution service."""
        return self.execution_worker_service.process_next_execution_job(worker_id)

    MAX_AGENT_CONTEXT_RECORDS = 32
    MAX_AGENT_CONTEXT_SERIALIZED_CHARS = 48_000
    MAX_WORKFLOW_REFERENCE_SERIALIZED_CHARS = 30_000
    STALE_RUN_AFTER_SECONDS = _runtime_constants.STALE_RUN_AFTER_SECONDS
    STALE_EXECUTION_JOB_AFTER_SECONDS = (
        _runtime_constants.STALE_EXECUTION_JOB_AFTER_SECONDS
    )
    STALE_ACTION_INVOCATION_AFTER_SECONDS = (
        _runtime_constants.STALE_ACTION_INVOCATION_AFTER_SECONDS
    )
    EXECUTION_JOB_HEARTBEAT_SECONDS = _runtime_constants.EXECUTION_JOB_HEARTBEAT_SECONDS

    @property
    def http_client(self) -> httpx.Client:
        """Return the process HTTP client shared by model and OAuth calls."""
        client = self._model_http_client
        if client is None:
            raise RuntimeError("HTTP client is closed")
        return client

    def close(self) -> None:
        """Release clients owned by this runtime and its composition root."""
        if self._model_http_client is not None:
            self._model_http_client.close()
            self._model_http_client = None
        close_storage = self._close_storage
        self._close_storage = lambda: None
        close_storage()

    def check_storage(self) -> None:
        """Check readiness of the selected durable store, if configured."""
        self._check_storage()
