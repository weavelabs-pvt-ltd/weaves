"""Governed dispatch for registered context-read capabilities."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from contracts.v1 import (
    AgentVersion,
    CapabilityKind,
    CapabilitySpec,
    InvocationStatus,
    PluginDescriptorStatus,
    PluginInstallation,
    PluginInstallationBinding,
    PluginInstallationStatus,
)
from contracts.v1.base import OpaqueId
from contracts.v1.json_data import (
    freeze_json_value,
    reject_credential_fields,
    thaw_json_value,
)
from infrastructure.persistence.repositories import Repository
from shared.errors import PluginGatewayError as PluginGatewayError


class CapabilityProvider(Protocol):
    def invoke(self, capability_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class InstallationAwareCapabilityProvider(Protocol):
    def invoke_for_installation(
        self,
        capability_id: str,
        payload: dict[str, Any],
        installation: Any,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CapabilityResult:
    output: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    status: InvocationStatus = InvocationStatus.SUCCEEDED


class PluginGateway:
    """Resolve, authorize, schema-check, and dispatch a capability invocation."""

    def __init__(
        self,
        descriptors: Repository[Any],
        installations: Repository[Any],
        providers: Mapping[str, Any],
    ) -> None:
        self._descriptors = descriptors
        self._installations = installations
        self._providers = dict(providers)

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
    ) -> CapabilityResult:
        """Validate a requested capability and dispatch it to its provider."""
        binding = self._resolve_binding(
            org_id, workspace_id, installation_id, agent_version, capability_id
        )
        capability = self._resolve_capability(
            binding, capability_id, agent_version, payload, approval_verified
        )
        provider = self._resolve_provider(binding)
        output, started_at, started = self._call_provider(
            provider, capability_id, payload, binding.installation
        )
        return self._build_result(capability, output, started_at, started)

    def _resolve_binding(
        self,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
        installation_id: OpaqueId,
        agent_version: AgentVersion,
        capability_id: str,
    ) -> PluginInstallationBinding:
        """Load an active installation that is allowed by the agent version."""
        if agent_version.org_id != org_id or agent_version.workspace_id != workspace_id:
            raise PluginGatewayError(
                "plugin.scope_mismatch", "Agent is outside the requested scope"
            )
        try:
            installation = self._installations.get_scoped(
                installation_id, org_id, workspace_id
            )
        except KeyError as exc:
            raise PluginGatewayError(
                "plugin.installation_not_found", "Plugin installation was not found"
            ) from exc
        if installation.status is not PluginInstallationStatus.ACTIVE:
            raise PluginGatewayError(
                "plugin.installation_disabled", "Plugin installation is disabled"
            )
        try:
            descriptor = self._descriptors.get(installation.plugin_id)
        except KeyError as exc:
            raise PluginGatewayError(
                "plugin.descriptor_not_found", "Plugin descriptor was not found"
            ) from exc
        if descriptor.status is not PluginDescriptorStatus.ACTIVE:
            raise PluginGatewayError("plugin.descriptor_disabled", "Plugin is disabled")
        try:
            binding = PluginInstallationBinding(
                descriptor=descriptor, installation=installation
            )
        except ValidationError as exc:
            raise PluginGatewayError(
                "plugin.binding_invalid", "Plugin installation is invalid"
            ) from exc
        if capability_id not in agent_version.allowed_capability_ids:
            raise PluginGatewayError(
                "plugin.capability_denied",
                "Agent is not allowed to use this capability",
            )
        if capability_id not in installation.enabled_capability_ids:
            raise PluginGatewayError(
                "plugin.capability_disabled",
                "Capability is not enabled for this installation",
            )
        if descriptor.configuration_schema is not None:
            try:
                _validate_payload(
                    descriptor.configuration_schema,
                    thaw_json_value(installation.configuration),
                    "configuration",
                )
            except PluginGatewayError as exc:
                raise PluginGatewayError(
                    "plugin.configuration_invalid",
                    "Plugin installation configuration is invalid",
                ) from exc
        return binding

    def _resolve_capability(
        self,
        binding: PluginInstallationBinding,
        capability_id: str,
        agent_version: AgentVersion,
        payload: dict[str, Any],
        approval_verified: bool,
    ) -> CapabilitySpec:
        """Confirm capability policy and validate the provider input schema."""
        capability = next(
            (
                item
                for item in binding.descriptor.capability_manifest
                if item.capability_id == capability_id
            ),
            None,
        )
        if capability is None:
            raise PluginGatewayError(
                "plugin.capability_unknown", "Capability is not registered"
            )
        if capability.kind is CapabilityKind.CONTEXT_READ or (
            capability.kind is CapabilityKind.ACTION_WRITE
            and capability.approval_supported
            and approval_verified
        ):
            pass
        else:
            raise PluginGatewayError(
                "plugin.action_not_supported",
                "This capability requires an approved action request",
            )
        _validate_payload(capability.input_schema, payload, "input")
        return capability

    def _resolve_provider(self, binding: PluginInstallationBinding) -> Any:
        """Resolve a capability provider by plugin ID or registered type."""
        descriptor = binding.descriptor
        provider = self._providers.get(descriptor.plugin_id) or self._providers.get(
            descriptor.plugin_type
        )
        if provider is None:
            raise PluginGatewayError(
                "plugin.provider_missing", "No provider is registered for this plugin"
            )
        return provider

    @staticmethod
    def _call_provider(
        provider: Any,
        capability_id: str,
        payload: dict[str, Any],
        installation: PluginInstallation,
    ) -> tuple[dict[str, Any], datetime, float]:
        """Call an installation-aware provider and capture timing metadata."""
        started_at = datetime.now(UTC)
        started = perf_counter()
        try:
            installation_dispatch = getattr(provider, "invoke_for_installation", None)
            if callable(installation_dispatch):
                output = installation_dispatch(capability_id, payload, installation)
            else:
                output = provider.invoke(capability_id, payload)
        except PluginGatewayError:
            raise
        except Exception as exc:
            raise PluginGatewayError(
                "plugin.provider_error", "Capability provider failed"
            ) from exc
        return output, started_at, started

    @staticmethod
    def _build_result(
        capability: CapabilitySpec,
        output: Any,
        started_at: datetime,
        started: float,
    ) -> CapabilityResult:
        """Validate provider output and construct a normalized result."""
        finished_at = datetime.now(UTC)
        if not isinstance(output, dict):
            raise PluginGatewayError(
                "plugin.output_invalid", "Capability returned an invalid output"
            )
        try:
            _validate_payload(capability.output_schema, output, "output")
        except PluginGatewayError as exc:
            raise PluginGatewayError(
                "plugin.output_invalid", "Capability returned an invalid output"
            ) from exc
        return CapabilityResult(
            output=thaw_json_value(freeze_json_value(output, "output")),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )

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
        """Validate an action proposal without dispatching it to the provider."""
        if agent_version.org_id != org_id or agent_version.workspace_id != workspace_id:
            raise PluginGatewayError(
                "plugin.scope_mismatch", "Agent is outside the requested scope"
            )
        if capability_id not in agent_version.allowed_capability_ids:
            raise PluginGatewayError(
                "plugin.capability_denied",
                "Agent is not allowed to propose this action",
            )
        try:
            installation = self._installations.get_scoped(
                installation_id, org_id, workspace_id
            )
        except KeyError as exc:
            raise PluginGatewayError(
                "plugin.installation_not_found", "Plugin installation was not found"
            ) from exc
        if installation.status is not PluginInstallationStatus.ACTIVE:
            raise PluginGatewayError(
                "plugin.installation_disabled", "Plugin installation is disabled"
            )
        if capability_id not in installation.enabled_capability_ids:
            raise PluginGatewayError(
                "plugin.capability_disabled",
                "Capability is not enabled for this installation",
            )
        try:
            descriptor = self._descriptors.get(installation.plugin_id)
            binding = PluginInstallationBinding(
                descriptor=descriptor, installation=installation
            )
        except (KeyError, ValidationError) as exc:
            raise PluginGatewayError(
                "plugin.binding_invalid", "Plugin installation is invalid"
            ) from exc
        if descriptor.status is not PluginDescriptorStatus.ACTIVE:
            raise PluginGatewayError("plugin.descriptor_disabled", "Plugin is disabled")
        if descriptor.configuration_schema is not None:
            try:
                _validate_payload(
                    descriptor.configuration_schema,
                    thaw_json_value(installation.configuration),
                    "configuration",
                )
            except PluginGatewayError as exc:
                raise PluginGatewayError(
                    "plugin.configuration_invalid",
                    "Plugin installation configuration is invalid",
                ) from exc
        capability = next(
            (
                item
                for item in binding.descriptor.capability_manifest
                if item.capability_id == capability_id
            ),
            None,
        )
        if (
            capability is None
            or capability.kind is not CapabilityKind.ACTION_WRITE
            or not capability.approval_supported
        ):
            raise PluginGatewayError(
                "plugin.action_not_supported",
                "This action is not available for approval",
            )
        _validate_payload(capability.input_schema, payload, "input")
        return installation, capability


def _validate_payload(
    schema: dict[str, Any], payload: dict[str, Any], label: str
) -> None:
    try:
        reject_credential_fields(payload, label)
        freeze_json_value(payload, label)
        limit = 16_384 if label == "input" else 65_536
        if len(json.dumps(payload, allow_nan=False)) > limit:
            raise ValueError(f"{label} exceeds size limit")
        Draft202012Validator(schema).validate(payload)
    except Exception as exc:
        raise PluginGatewayError(
            f"plugin.{label}_invalid", f"Capability {label} is invalid"
        ) from exc
