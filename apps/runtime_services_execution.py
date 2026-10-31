"""Construct capability services at the API and worker composition boundary."""

from __future__ import annotations

from application.runtime_constants import EXECUTION_JOB_HEARTBEAT_SECONDS
from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.agents.defaults import default_active_agent_id
from modules.agents.execution_service import AgentExecutionService
from modules.evaluations.service import EvaluationService
from modules.executions.recovery_service import ExecutionRecoveryService
from modules.executions.service import ExecutionService
from modules.executions.worker_service import ExecutionWorkerService
from modules.governance.action_recovery_service import ActionRecoveryService
from modules.workflows.execution_service import WorkflowExecutionService


def _configure_execution_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Construct durable execution with repository and platform ports."""
    services.execution_service = ExecutionService(
        agents=dependencies.agents,
        agent_versions=dependencies.agent_versions,
        audit_events=dependencies.audit_events,
        evaluation_executions=dependencies.evaluation_executions,
        execution_jobs=dependencies.execution_jobs,
        threads=dependencies.threads,
        workflows=dependencies.workflows,
        workflow_versions=dependencies.workflow_versions,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        default_agent_id=lambda: default_active_agent_id(dependencies.agents),
        unit_of_work=dependencies._unit_of_work,
        idempotency_guard=dependencies._idempotency_guard,
        execution_job_idempotency_locks=(dependencies._execution_job_idempotency_locks),
    )


def _configure_agent_execution_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose agent execution with model, memory, and integration ports."""
    services.agent_execution_service = AgentExecutionService(
        agents=dependencies.agents,
        agent_runs=dependencies.agent_runs,
        agent_versions=dependencies.agent_versions,
        artifacts=dependencies.artifacts,
        audit_events=dependencies.audit_events,
        memory_items=dependencies.memory_items,
        model_profiles=dependencies.model_profiles,
        providers=dependencies.providers,
        runs=dependencies.runs,
        threads=dependencies.threads,
        tool_invocations=dependencies.tool_invocations,
        workflow_versions=dependencies.workflow_versions,
        workflows=dependencies.workflows,
        model_gateway=dependencies.model_gateway,
        plugin_gateway=dependencies.plugin_gateway,
        memory_context=dependencies.memory_context,
        integration_admin_service=services.integration_admin_service,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        unit_of_work=dependencies._unit_of_work,
        idempotency_guard=dependencies._idempotency_guard,
        idempotency_locks=dependencies._idempotency_locks,
        default_agent_id=lambda: default_active_agent_id(dependencies.agents),
    )


def _configure_workflow_execution_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose workflow execution with shared agent and run operations."""
    services.workflow_execution_service = WorkflowExecutionService(
        runs=dependencies.runs,
        workflows=dependencies.workflows,
        workflow_versions=dependencies.workflow_versions,
        agent_versions=dependencies.agent_versions,
        agents=dependencies.agents,
        model_profiles=dependencies.model_profiles,
        providers=dependencies.providers,
        threads=dependencies.threads,
        audit_events=dependencies.audit_events,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        unit_of_work=dependencies._unit_of_work,
        idempotency_guard=dependencies._idempotency_guard,
        idempotency_locks=dependencies._idempotency_locks,
        execute_agent_run=services.agent_execution_service._execute_agent_run,
        replay_idempotent_run=(services.agent_execution_service._replay_idempotent_run),
    )


def _configure_execution_worker_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Construct the durable worker with execution and evaluation callbacks."""
    services.execution_worker_service = ExecutionWorkerService(
        audit_events=dependencies.audit_events,
        execution_jobs=dependencies.execution_jobs,
        runs=dependencies.runs,
        evaluation_service=services.evaluation_service,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        unit_of_work=dependencies._unit_of_work,
        heartbeat_execution_job=services.recovery_service.heartbeat_execution_job,
        run_agent=services.agent_execution_service.run_agent,
        run_workflow=services.workflow_execution_service.run_workflow,
        heartbeat_interval_seconds=EXECUTION_JOB_HEARTBEAT_SECONDS,
    )


def _configure_evaluation_and_recovery_services(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Construct evaluation execution and stale-work recovery services."""
    services.evaluation_service = EvaluationService(
        agents=dependencies.agents,
        agent_runs=dependencies.agent_runs,
        artifacts=dependencies.artifacts,
        audit_events=dependencies.audit_events,
        evaluation_executions=dependencies.evaluation_executions,
        evaluation_reports=dependencies.evaluation_reports,
        evaluation_suites=dependencies.evaluation_suites,
        execution_jobs=dependencies.execution_jobs,
        runs=dependencies.runs,
        tool_invocations=dependencies.tool_invocations,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        unit_of_work=dependencies._unit_of_work,
        cancel_execution_job=services.execution_service.cancel_execution_job,
        cancellation_requested=(
            lambda job_id, event: (
                services.execution_worker_service._execution_cancellation_requested(
                    job_id, event
                )
            )
        ),
        run_agent=services.agent_execution_service.run_agent,
    )
    services.recovery_service = ExecutionRecoveryService(
        runs=dependencies.runs,
        agent_runs=dependencies.agent_runs,
        audit_events=dependencies.audit_events,
        execution_jobs=dependencies.execution_jobs,
        evaluation_executions=dependencies.evaluation_executions,
        evaluation_service=services.evaluation_service,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        unit_of_work=dependencies._unit_of_work,
    )
    services.action_recovery_service = ActionRecoveryService(
        tool_invocations=dependencies.tool_invocations,
        approvals=dependencies.approvals,
        audit_events=dependencies.audit_events,
        unit_of_work=dependencies._unit_of_work,
    )
