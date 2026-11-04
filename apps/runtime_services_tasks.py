"""Construct the task capability from repositories and agent execution."""

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.tasks.service import TaskService
from modules.tasks.worker_service import TaskWorkerService


def _configure_task_services(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Build task APIs and the independent durable task-turn worker."""
    services.task_service = TaskService(
        agents=dependencies.agents,
        agent_versions=dependencies.agent_versions,
        threads=dependencies.threads,
        tasks=dependencies.tasks,
        messages=dependencies.task_messages,
        turns=dependencies.task_turns,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        unit_of_work=dependencies._unit_of_work,
    )
    services.task_worker_service = TaskWorkerService(
        tasks=dependencies.tasks,
        messages=dependencies.task_messages,
        turns=dependencies.task_turns,
        agent_runs=dependencies.agent_runs,
        runs=dependencies.runs,
        agent_execution=services.agent_execution_service,
        unit_of_work=dependencies._unit_of_work,
    )
