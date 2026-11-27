"""Is an AI model connected? One answer for the dispatcher, the planner and the reply.

The owner's rule: when the model is not connected, or no AI connectivity is
present, the bot always says so. "Not connected" is decided from records and
error classes only, never from display names or provider text:

* the organization has no active default model profile;
* the default profile or its provider is disabled;
* the provider is the built-in test placeholder (``ModelProviderType.LOCAL``, the
  "Deterministic mock" seeded into every new organization): it is not a model;
* the provider has no credential reference.

A model that is configured but fails while in use (authentication refused, an
invalid or expired key, the provider unreachable or timing out, the adapter not
configured) is the same situation for the asker; ``is_connectivity_failure``
maps those error classes. A response the model returned but nobody can use is
not a connectivity problem and keeps its own message.
"""

from __future__ import annotations

import re
from enum import StrEnum

from application.ports.repository import Repository
from contracts.v1 import (
    ModelProfile,
    ModelProfileStatus,
    ModelProvider,
    ModelProviderStatus,
    ModelProviderType,
    OrganizationModelConfiguration,
)
from modules.models.defaults import default_active_model_profile


class ModelReadiness(StrEnum):
    """Whether a model can answer, and if not, why (codes for logs and tests)."""

    CONNECTED = "connected"
    NO_PROFILE = "no_profile"
    TEST_PROVIDER = "test_provider"
    NO_CREDENTIAL = "no_credential"
    DISABLED = "disabled"

    @property
    def connected(self) -> bool:
        """Whether a real model is connected."""
        return self is ModelReadiness.CONNECTED


# The provider answered nothing usable because it cannot be reached or trusted.
_CONNECTIVITY_CODES = frozenset(
    {
        "model.credential_unavailable",
        "model.auth_ref_missing",
        "model.adapter_missing",
        "model.disabled",
        "model.provider_disabled",
        "model.provider_unsupported",
        "model.mode_unavailable",
        "model.egress_denied",
        "model.redirect_blocked",
        "model.timeout",
        "model.transport_error",
    }
)
# Upstream refusals that mean the key or the route is bad (not a bad request).
_CONNECTIVITY_STATUSES = frozenset({401, 403, 407, 502, 503, 504})
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")


def is_connectivity_failure(
    code: str, http_status: int | None = None, summary: str = ""
) -> bool:
    """Whether a model error code means "no usable AI connectivity".

    ``summary`` is only searched for the upstream status the gateway wrote into it
    (``Model provider returned HTTP 401``) when the status was not kept; no
    provider text is ever copied anywhere.
    """
    if code in _CONNECTIVITY_CODES:
        return True
    if code != "model.upstream_error":
        return False
    if http_status is None:
        found = _HTTP_STATUS.search(summary)
        http_status = int(found.group(1)) if found else None
    return http_status in _CONNECTIVITY_STATUSES


def provider_readiness(
    profile: ModelProfile, provider: ModelProvider
) -> ModelReadiness:
    """Readiness of one profile and its provider."""
    if (
        profile.status is not ModelProfileStatus.ACTIVE
        or provider.status is not ModelProviderStatus.ACTIVE
    ):
        return ModelReadiness.DISABLED
    if provider.provider_type is ModelProviderType.LOCAL:
        return ModelReadiness.TEST_PROVIDER
    if not (provider.auth_ref or "").strip():
        return ModelReadiness.NO_CREDENTIAL
    return ModelReadiness.CONNECTED


class ModelConnectivity:
    """Reads an organization's default model and says whether it is connected."""

    def __init__(
        self,
        *,
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProvider],
        model_configurations: Repository[OrganizationModelConfiguration],
    ) -> None:
        """Bind the model records of the organizations."""
        self._profiles = model_profiles
        self._providers = providers
        self._configurations = model_configurations

    def readiness(self, org_id: str) -> ModelReadiness:
        """The state of the organization's default model profile."""
        try:
            profile = default_active_model_profile(
                self._profiles, self._configurations, org_id
            )
            provider = self._providers.get_scoped(profile.provider_id, org_id)
        except (ValueError, KeyError):
            return ModelReadiness.NO_PROFILE
        return provider_readiness(profile, provider)

    def connected(self, org_id: str) -> bool:
        """Whether the organization has a real, enabled, credentialed model."""
        return self.readiness(org_id).connected
