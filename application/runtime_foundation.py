"""Bind process resources and repositories to the compatibility runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from threading import RLock

import httpx

from application.ports.email import EmailSender
from application.ports.product_repositories import ProductRepositorySet
from application.ports.runtime_adapters import RuntimeAdapterBundle
from application.ports.secrets import SecretStore
from application.ports.throttling import LoginRateLimiter
from application.runtime_coordination import RuntimeCoordinationMixin
from application.runtime_repositories import RuntimeRepositoryBindingMixin
from clients.model_providers.gateway import ModelAdapter
from contracts.v1 import ModelProviderType
from modules.auth.runtime_organizations import MockModelIdentifier


class RuntimeFoundationMixin(RuntimeRepositoryBindingMixin, RuntimeCoordinationMixin):
    """Bind long-lived adapters and repository ports without creating services."""

    model: MockModelIdentifier
    model_adapters: Mapping[ModelProviderType, ModelAdapter]
    email_sender: EmailSender
    email_login_hmac_key: str

    def __init__(
        self,
        *,
        http_client: httpx.Client,
        storage_mode: str,
        repository_set: ProductRepositorySet,
        secret_store: SecretStore,
        login_rate_limiter: LoginRateLimiter,
        onboarding_rate_limiter: LoginRateLimiter,
        runtime_adapters: RuntimeAdapterBundle,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        advisory_transaction_lock: Callable[[str], None] | None,
        close_storage: Callable[[], None],
        check_storage: Callable[[], None],
    ) -> None:
        """Bind application ports and adapters built by a process composition root."""
        self.storage_mode = storage_mode
        self.secret_store = secret_store
        self.login_rate_limiter = login_rate_limiter
        self.onboarding_rate_limiter = onboarding_rate_limiter
        self._unit_of_work_impl = unit_of_work
        self._advisory_transaction_lock = advisory_transaction_lock
        self._close_storage = close_storage
        self._check_storage = check_storage
        self._model_http_client: httpx.Client | None = http_client
        self._repository_set = repository_set
        self._bind_repositories()
        self._configure_coordination()
        self._bind_runtime_adapters(runtime_adapters)
        self._bootstrapped = False
        self._approval_lock = RLock()

    def _bind_runtime_adapters(self, adapters: RuntimeAdapterBundle) -> None:
        """Attach clients and gateways composed by the API or worker process."""
        self.model = adapters.model
        self.model_adapters = adapters.model_adapters
        self.model_gateway = adapters.model_gateway
        self.plugin_gateway = adapters.plugin_gateway
        self.github_plugin = adapters.github_plugin
        self.slack_plugin = adapters.slack_plugin
        self.jira_plugin = adapters.jira_plugin
        self.mcp_plugin = adapters.mcp_plugin
        self.oauth_token_resolver = adapters.oauth_token_resolver
        self.github_app_token_provider = adapters.github_app_token_provider
        self.memory_context = adapters.memory_context
        self.default_model_profile_id = adapters.default_model_profile_id
        self.environment_model_provider_type = adapters.environment_model_provider_type
        self.environment_model_name = adapters.environment_model_name
        self.environment_model_base_url = adapters.environment_model_base_url
        self.environment_model_enabled = adapters.environment_model_enabled
        self.openai_enabled = adapters.openai_enabled
        self.openai_model_name = adapters.openai_model_name
        self.openai_base_url = adapters.openai_base_url
        self.email_sender = adapters.email_sender
        self.email_login_hmac_key = adapters.email_login_hmac_key

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the transaction supplied by the process composition root."""
        return self._unit_of_work_impl()
