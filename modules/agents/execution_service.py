"""Run agents through explicit repositories, adapters, and application ports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import RLock

from application.ports.repository import Repository
from application.ports.runtime_adapters import PluginDispatcher
from clients.model_providers.gateway import ModelGateway
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    Artifact,
    AuditEvent,
    ConversationThread,
    MemoryItem,
    ModelProfile,
    Permission,
    ToolInvocation,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.agents.runtime_run_capabilities import AgentRunCapabilitiesMixin
from modules.agents.runtime_run_entrypoint import AgentRunEntrypointMixin
from modules.agents.runtime_run_errors import AgentRunErrorMixin
from modules.agents.runtime_run_lifecycle import AgentRunLifecycleMixin
from modules.agents.runtime_run_model import AgentRunModelMixin
from modules.agents.runtime_run_targets import AgentRunTargetsMixin
from modules.integrations.admin_service import IntegrationAdminService
from modules.memory.context import MemoryContextBuilder
from shared.clock import utc_now as _now


class AgentExecutionService(
    AgentRunModelMixin,
    AgentRunErrorMixin,
    AgentRunCapabilitiesMixin,
    AgentRunTargetsMixin,
    AgentRunLifecycleMixin,
    AgentRunEntrypointMixin,
):
    """Coordinate agent context, model calls, persistence, and replay."""

    agents: Repository[AgentDefinition]
    agent_runs: Repository[AgentRun]
    agent_versions: Repository[AgentVersion]
    artifacts: Repository[Artifact]
    audit_events: Repository[AuditEvent]
    memory_items: Repository[MemoryItem]
    model_profiles: Repository[ModelProfile]
    providers: Repository[ModelProviderContract]
    model_gateway: ModelGateway
    plugin_gateway: PluginDispatcher
    memory_context: MemoryContextBuilder
    integration_admin_service: IntegrationAdminService
    runs: Repository[WorkflowRun]
    threads: Repository[ConversationThread]
    tool_invocations: Repository[ToolInvocation]
    workflow_versions: Repository[WorkflowVersion]
    workflows: Repository[WorkflowDefinition]

    MAX_AGENT_CONTEXT_RECORDS = 32
    MAX_AGENT_CONTEXT_SERIALIZED_CHARS = 48_000
    MAX_WORKFLOW_REFERENCE_SERIALIZED_CHARS = 30_000

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_runs: Repository[AgentRun],
        agent_versions: Repository[AgentVersion],
        artifacts: Repository[Artifact],
        audit_events: Repository[AuditEvent],
        memory_items: Repository[MemoryItem],
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProviderContract],
        runs: Repository[WorkflowRun],
        threads: Repository[ConversationThread],
        tool_invocations: Repository[ToolInvocation],
        workflow_versions: Repository[WorkflowVersion],
        workflows: Repository[WorkflowDefinition],
        model_gateway: ModelGateway,
        plugin_gateway: PluginDispatcher,
        memory_context: MemoryContextBuilder,
        integration_admin_service: IntegrationAdminService,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        idempotency_guard: RLock,
        idempotency_locks: dict[tuple[str, str], RLock],
        default_agent_id: Callable[[], str],
    ) -> None:
        """Bind agent persistence, shared clients, and narrow runtime callbacks."""
        self.agents = agents
        self.agent_runs = agent_runs
        self.agent_versions = agent_versions
        self.artifacts = artifacts
        self.audit_events = audit_events
        self.memory_items = memory_items
        self.model_profiles = model_profiles
        self.providers = providers
        self.runs = runs
        self.threads = threads
        self.tool_invocations = tool_invocations
        self.workflow_versions = workflow_versions
        self.workflows = workflows
        self.model_gateway = model_gateway
        self.plugin_gateway = plugin_gateway
        self.memory_context = memory_context
        self.integration_admin_service = integration_admin_service
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work
        self._idempotency_guard = idempotency_guard
        self._idempotency_locks = idempotency_locks
        self._default_agent_id_impl = default_agent_id

    def is_bootstrapped(self) -> bool:
        """Return current process bootstrap state."""
        return self._is_bootstrapped()

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce agent run permissions through the identity capability."""
        self._authorize(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for agent execution persistence."""
        return self._open_unit_of_work()

    def _default_agent_id(self) -> str:
        """Resolve the configured default agent through the platform port."""
        return self._default_agent_id_impl()

    def _touch_run_heartbeat(self, run: WorkflowRun) -> WorkflowRun:
        """Persist liveness before an external agent call."""
        updated = WorkflowRun.model_validate(
            run.model_copy(update={"updated_at": _now()}).model_dump()
        )
        return self.runs.put(updated)
