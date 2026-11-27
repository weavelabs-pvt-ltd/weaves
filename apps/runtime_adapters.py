"""Build provider clients and persistence-backed secret adapters for processes."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from application.ports.product_repositories import ProductRepositorySet
from application.ports.runtime_adapters import RuntimeAdapterBundle
from application.ports.secrets import SecretStore
from application.ports.slack import SlackStore
from application.ports.throttling import LoginRateLimiter
from application.ports.usage import UsageLedger
from clients.integrations.github_app_auth import GitHubAppTokenProvider
from clients.integrations.github_pr_review import GitHubReviewPlugin
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
from infrastructure.persistence.deterministic_model import (
    DeterministicMockModel,
)
from infrastructure.secrets.store import (
    CompositeSecretResolver,
    EncryptedPostgresSecretStore,
    InMemorySecretStore,
    UnavailableSecretStore,
)
from modules.recovery.secret_tombstones import TombstonedSecretStore
from modules.recovery.tombstones import NullTombstones, tombstones_from_env
from shared.github_config import github_api_base_url_from_environment


def build_secret_store(store: ProductPostgresStore | None) -> SecretStore:
    """Select encrypted database or process-memory secret storage."""
    encryption_key = os.environ.get("WEAVES_SECRET_ENCRYPTION_KEY", "").strip()
    if store is None:
        return InMemorySecretStore()
    if not encryption_key:
        return UnavailableSecretStore()
    durable = EncryptedPostgresSecretStore(store, encryption_key)
    sink = tombstones_from_env()
    if isinstance(sink, NullTombstones):
        return durable
    return TombstonedSecretStore(durable, sink)


def build_login_rate_limiter(store: ProductPostgresStore | None) -> LoginRateLimiter:
    """Build the process-appropriate account sign-in throttler."""
    from infrastructure.auth.throttle import (
        InMemoryLoginRateLimiter,
        PostgresLoginRateLimiter,
    )

    if store is None:
        return InMemoryLoginRateLimiter()
    return PostgresLoginRateLimiter(store.pool)


def build_usage_ledger(store: ProductPostgresStore | None) -> UsageLedger:
    """Select the PostgreSQL usage ledger or the process-local development one."""
    from infrastructure.persistence.usage_ledger import PostgresUsageLedger
    from infrastructure.persistence.usage_ledger_memory import InMemoryUsageLedger

    return InMemoryUsageLedger() if store is None else PostgresUsageLedger(store)


def build_slack_store(store: ProductPostgresStore | None) -> SlackStore:
    """Select the PostgreSQL Slack store or the process-local development one."""
    from infrastructure.persistence.slack_store import PostgresSlackStore
    from infrastructure.persistence.slack_store_memory import InMemorySlackStore

    return InMemorySlackStore() if store is None else PostgresSlackStore(store)


def build_runtime_adapters(
    *,
    http_client: httpx.Client,
    provider_http_client: httpx.Client,
    secret_store: SecretStore,
    repositories: ProductRepositorySet,
) -> RuntimeAdapterBundle:
    """Create model adapters over guarded egress and reuse the integration client."""
    model, model_settings, adapters = _build_model_adapters(
        provider_http_client, secret_store
    )
    oauth_resolver = OAuthTokenResolver(secret_store, http_client)
    github_base = github_api_base_url_from_environment()
    github_app_provider = GitHubAppTokenProvider(http_client, api_base_url=github_base)
    github_plugin = GitHubReviewPlugin(
        http_client,
        api_base_url=github_base,
        installation_token_provider=github_app_provider,
    )
    slack_plugin = SlackReadPlugin(oauth_resolver, http_client)
    jira_plugin = JiraReadPlugin(oauth_resolver, http_client)
    mcp_plugin = McpHttpPlugin(oauth_resolver, http_client)
    plugin_gateway = PluginGateway(
        repositories.get("plugins"),
        repositories.get("plugin_installations"),
        {
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
    if enabled:
        from clients.model_providers.egress_policy import ModelEgressPolicy

        base_url = ModelEgressPolicy.from_environment().base_url(
            provider_type, base_url
        )
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
