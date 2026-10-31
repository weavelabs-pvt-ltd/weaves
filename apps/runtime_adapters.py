"""Build provider clients and persistence-backed secret adapters for processes."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from application.ports.product_repositories import ProductRepositorySet
from application.ports.runtime_adapters import RuntimeAdapterBundle
from application.ports.secrets import SecretStore
from application.ports.throttling import LoginRateLimiter
from clients.integrations.github import GitHubReadPlugin
from clients.integrations.github_app_auth import GitHubAppTokenProvider
from clients.integrations.jira import JiraReadPlugin
from clients.integrations.mcp import McpHttpPlugin
from clients.integrations.oauth import OAuthTokenResolver
from clients.integrations.slack import SlackReadPlugin
from clients.model_providers.gateway import (
    AnthropicAdapter,
    GeminiAdapter,
    ModelAdapter,
    ModelGateway,
    OpenAICompatibleAdapter,
)
from contracts.v1 import ModelProviderType
from infrastructure.database.store import ProductPostgresStore
from infrastructure.integrations.plugin_gateway import PluginGateway
from infrastructure.persistence.memory import (
    DeterministicMockModel,
    RepositoryKnowledgePlugin,
)
from infrastructure.secrets.store import (
    CompositeSecretResolver,
    EncryptedPostgresSecretStore,
    InMemorySecretStore,
    UnavailableSecretStore,
)
from modules.memory.context import MemoryContextBuilder


def build_secret_store(store: ProductPostgresStore | None) -> SecretStore:
    """Select encrypted database or process-memory secret storage."""
    encryption_key = os.environ.get("WEAVES_SECRET_ENCRYPTION_KEY", "").strip()
    if store is None:
        return InMemorySecretStore()
    if not encryption_key:
        return UnavailableSecretStore()
    return EncryptedPostgresSecretStore(store.pool, encryption_key)


def build_login_rate_limiters(
    store: ProductPostgresStore | None,
) -> tuple[LoginRateLimiter, LoginRateLimiter]:
    """Build process-appropriate login and onboarding throttlers."""
    from infrastructure.auth.throttle import (
        InMemoryLoginRateLimiter,
        PostgresLoginRateLimiter,
    )

    if store is None:
        return (
            InMemoryLoginRateLimiter(),
            InMemoryLoginRateLimiter(limit=3, window_seconds=3600),
        )
    return (
        PostgresLoginRateLimiter(store.pool),
        PostgresLoginRateLimiter(store.pool, limit=3, window_seconds=3600),
    )


def build_runtime_adapters(
    *,
    http_client: httpx.Client,
    secret_store: SecretStore,
    repositories: ProductRepositorySet,
) -> RuntimeAdapterBundle:
    """Create model and integration adapters over one shared HTTP client."""
    model, model_settings, adapters = _build_model_adapters(http_client, secret_store)
    oauth_resolver = OAuthTokenResolver(secret_store, http_client)
    github_app_provider = GitHubAppTokenProvider(http_client)
    secret_resolver = CompositeSecretResolver(secret_store)
    github_plugin = GitHubReadPlugin(
        secret_resolver,
        http_client,
        installation_token_provider=github_app_provider,
    )
    slack_plugin = SlackReadPlugin(oauth_resolver, http_client)
    jira_plugin = JiraReadPlugin(oauth_resolver, http_client)
    mcp_plugin = McpHttpPlugin(secret_resolver, http_client)
    knowledge_plugin = RepositoryKnowledgePlugin(
        repositories.get("knowledge_documents")
    )
    plugin_gateway = PluginGateway(
        repositories.get("plugins"),
        repositories.get("plugin_installations"),
        {
            "local-knowledge": knowledge_plugin,
            "github": github_plugin,
            "slack": slack_plugin,
            "jira": jira_plugin,
            "mcp": mcp_plugin,
        },
    )
    return RuntimeAdapterBundle(
        model=model,
        model_adapters=adapters,
        model_gateway=ModelGateway(adapters),
        plugin_gateway=plugin_gateway,
        github_plugin=github_plugin,
        slack_plugin=slack_plugin,
        jira_plugin=jira_plugin,
        mcp_plugin=mcp_plugin,
        oauth_token_resolver=oauth_resolver,
        github_app_token_provider=github_app_provider,
        memory_context=MemoryContextBuilder(),
        default_model_profile_id=model_settings.default_model_profile_id,
        environment_model_provider_type=model_settings.provider_type,
        environment_model_name=model_settings.model_name,
        environment_model_base_url=model_settings.base_url,
        environment_model_enabled=model_settings.enabled,
    )


def _build_model_adapters(
    http_client: httpx.Client, secret_store: SecretStore
) -> tuple[
    DeterministicMockModel,
    _ModelSettings,
    dict[ModelProviderType, ModelAdapter],
]:
    """Configure provider adapters and environment-selected model defaults."""
    model = DeterministicMockModel()
    model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini").strip() or "gpt-4o-mini"
    provider_value = os.environ.get("MODEL_PROVIDER_TYPE", "openai").strip()
    try:
        provider_type = ModelProviderType(provider_value)
    except ValueError as exc:
        raise ValueError(
            "MODEL_PROVIDER_TYPE must be one of: openai, anthropic, gemini, "
            "openai_compatible"
        ) from exc
    external_providers = {
        ModelProviderType.OPENAI,
        ModelProviderType.OPENAI_COMPATIBLE,
        ModelProviderType.ANTHROPIC,
        ModelProviderType.GEMINI,
    }
    if provider_type not in external_providers:
        raise ValueError(
            "MODEL_PROVIDER_TYPE must select a configured external provider"
        )
    base_urls = {
        ModelProviderType.OPENAI: "https://api.openai.com/v1",
        ModelProviderType.OPENAI_COMPATIBLE: "https://api.openai.com/v1",
        ModelProviderType.ANTHROPIC: "https://api.anthropic.com",
        ModelProviderType.GEMINI: "https://generativelanguage.googleapis.com/v1beta",
    }
    base_url = os.environ.get("MODEL_BASE_URL", "").strip() or base_urls[provider_type]
    enabled = bool(os.environ.get("MODEL_API_KEY", "").strip())
    default_profile_id = (
        "local-model-profile"
        if not enabled
        else "openai-model-profile"
        if provider_type is ModelProviderType.OPENAI
        else "configured-model-profile"
    )
    resolver = CompositeSecretResolver(secret_store)
    adapters: dict[ModelProviderType, ModelAdapter] = {
        ModelProviderType.LOCAL: model,
        ModelProviderType.OPENAI: OpenAICompatibleAdapter(resolver, http_client),
        ModelProviderType.OPENAI_COMPATIBLE: OpenAICompatibleAdapter(
            resolver, http_client
        ),
        ModelProviderType.ANTHROPIC: AnthropicAdapter(resolver, http_client),
        ModelProviderType.GEMINI: GeminiAdapter(resolver, http_client),
    }
    return (
        model,
        _ModelSettings(
            provider_type=provider_type,
            model_name=model_name,
            base_url=base_url,
            enabled=enabled,
            default_model_profile_id=default_profile_id,
        ),
        adapters,
    )


@dataclass(frozen=True)
class _ModelSettings:
    """Validated environment-selected model defaults."""

    provider_type: ModelProviderType
    model_name: str
    base_url: str
    enabled: bool
    default_model_profile_id: str
