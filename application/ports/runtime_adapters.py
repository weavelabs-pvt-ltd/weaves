"""Provider and integration adapter bundle consumed by the runtime facade."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from application.ports.email import EmailSender
from clients.integrations.github import GitHubReadPlugin
from clients.integrations.github_app_auth import GitHubAppTokenProvider
from clients.integrations.jira import JiraReadPlugin
from clients.integrations.mcp import McpHttpPlugin
from clients.integrations.oauth import OAuthTokenResolver
from clients.integrations.slack import SlackReadPlugin
from clients.model_providers.gateway import ModelAdapter, ModelGateway
from contracts.v1 import (
    AgentVersion,
    CapabilitySpec,
    InvocationStatus,
    ModelProviderType,
    PluginInstallation,
)
from contracts.v1.base import OpaqueId
from modules.auth.runtime_organizations import MockModelIdentifier
from modules.memory.context import MemoryContextBuilder


class PluginInvocationResult(Protocol):
    """Result fields consumed by agent execution and audit use cases."""

    @property
    def output(self) -> dict[str, Any]:
        """Return the normalized capability output."""
        ...

    @property
    def started_at(self) -> datetime:
        """Return when the external invocation began."""
        ...

    @property
    def finished_at(self) -> datetime:
        """Return when the external invocation finished."""
        ...

    @property
    def duration_ms(self) -> int:
        """Return the invocation duration in milliseconds."""
        ...

    @property
    def status(self) -> InvocationStatus:
        """Return the normalized invocation outcome."""
        ...


class PluginDispatcher(Protocol):
    """Governed dispatch operations used by application capabilities."""

    def invoke(
        self,
        *,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
        installation_id: OpaqueId,
        agent_version: AgentVersion,
        capability_id: str,
        payload: dict[str, Any],
        approval_verified: bool = False,
    ) -> PluginInvocationResult:
        """Validate and dispatch one capability invocation."""
        ...

    def validate_approved_action_request(
        self,
        *,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
        installation_id: OpaqueId,
        agent_version: AgentVersion,
        capability_id: str,
        payload: dict[str, Any],
    ) -> tuple[PluginInstallation, CapabilitySpec]:
        """Validate an approval-backed action without dispatching it."""
        ...


class RuntimeAdapterBundle:
    """Provider-agnostic adapter dependencies created by process wiring."""

    def __init__(
        self,
        *,
        email_sender: EmailSender,
        email_login_hmac_key: str,
        model: MockModelIdentifier,
        model_adapters: Mapping[ModelProviderType, ModelAdapter],
        model_gateway: ModelGateway,
        plugin_gateway: PluginDispatcher,
        github_plugin: GitHubReadPlugin,
        slack_plugin: SlackReadPlugin,
        jira_plugin: JiraReadPlugin,
        mcp_plugin: McpHttpPlugin,
        oauth_token_resolver: OAuthTokenResolver,
        github_app_token_provider: GitHubAppTokenProvider,
        memory_context: MemoryContextBuilder,
        default_model_profile_id: str,
        environment_model_provider_type: ModelProviderType,
        environment_model_name: str,
        environment_model_base_url: str,
        environment_model_enabled: bool,
    ) -> None:
        """Store the constructed clients and model selection settings."""
        self.email_sender = email_sender
        self.email_login_hmac_key = email_login_hmac_key
        self.model = model
        self.model_adapters = model_adapters
        self.model_gateway = model_gateway
        self.plugin_gateway = plugin_gateway
        self.github_plugin = github_plugin
        self.slack_plugin = slack_plugin
        self.jira_plugin = jira_plugin
        self.mcp_plugin = mcp_plugin
        self.oauth_token_resolver = oauth_token_resolver
        self.github_app_token_provider = github_app_token_provider
        self.memory_context = memory_context
        self.default_model_profile_id = default_model_profile_id
        self.environment_model_provider_type = environment_model_provider_type
        self.environment_model_name = environment_model_name
        self.environment_model_base_url = environment_model_base_url
        self.environment_model_enabled = environment_model_enabled
        self.openai_enabled = environment_model_enabled
        self.openai_model_name = environment_model_name
        self.openai_base_url = environment_model_base_url
