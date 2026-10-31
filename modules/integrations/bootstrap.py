"""Seed the built-in integrations, local installation, and approval policy."""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from application.ports.repository import Repository
from contracts.v1 import (
    ApprovalPolicy,
    CapabilityKind,
    CapabilitySpec,
    PluginDescriptor,
    PluginDescriptorStatus,
    PluginInstallation,
    PluginInstallationStatus,
    RiskLevel,
)
from contracts.v1.base import ProductContract
from modules.knowledge.context import read_context_search

TProduct = TypeVar("TProduct", bound=ProductContract)


class IntegrationBootstrapService:
    """Create built-in integration descriptors and local policy records."""

    def __init__(
        self,
        *,
        plugins: Repository[PluginDescriptor],
        installations: Repository[PluginInstallation],
        approval_policies: Repository[ApprovalPolicy],
    ) -> None:
        """Bind the repositories owned by integration and governance modules."""
        self.plugins = plugins
        self.installations = installations
        self.approval_policies = approval_policies

    @staticmethod
    def _seed(
        repository: Repository[TProduct], id_field: str, record: TProduct
    ) -> TProduct:
        """Create a deterministic starter record only when it is absent."""
        record_id = str(getattr(record, id_field))
        try:
            return repository.get(record_id)
        except KeyError:
            try:
                return repository.create(record)
            except ValueError:
                return repository.get(record_id)

    def seed(self, now: datetime) -> None:
        """Seed built-in connectors, local knowledge, and action policy."""
        jira_comment, jira_search = self._seed_builtin_capabilities()
        self._seed_github_plugin()
        self._seed_slack_plugin()
        self._seed_jira_plugin(jira_comment, jira_search)
        self._seed_local_installation_and_policy(now)

    def _seed_builtin_capabilities(
        self,
    ) -> tuple[CapabilitySpec, CapabilitySpec]:
        """Build the built-in knowledge and Jira capability specs."""
        search_capability = CapabilitySpec(
            capability_id="knowledge.search",
            name="Search local knowledge",
            kind=CapabilityKind.CONTEXT_READ,
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            timeout_seconds=5,
        )
        jira_comment = CapabilitySpec(
            capability_id="jira.issues.comment",
            name="Add a Jira issue comment",
            description="Add a comment to an issue after human approval.",
            kind=CapabilityKind.ACTION_WRITE,
            risk_level=RiskLevel.MEDIUM,
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "pattern": r"^[A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,8}$",
                    },
                    "comment": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "required": ["issue_key", "comment"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string", "maxLength": 64},
                    "issue_url": {"type": "string", "maxLength": 512},
                },
                "required": ["comment_id", "issue_url"],
                "additionalProperties": False,
            },
            timeout_seconds=30,
            approval_supported=True,
        )
        jira_search = read_context_search(
            "jira.issues.search",
            "Search Jira issues",
            "Search Jira Cloud issues in configured projects.",
        )
        self._seed(
            self.plugins,
            "plugin_id",
            PluginDescriptor(
                plugin_id="local-knowledge",
                plugin_type="knowledge_base",
                display_name="Local knowledge",
                capability_manifest=(search_capability,),
                status=PluginDescriptorStatus.ACTIVE,
            ),
        )
        return jira_comment, jira_search

    def _seed_github_plugin(self) -> None:
        """Seed the built-in GitHub plugin descriptor."""
        github_search = CapabilitySpec(
            capability_id="github.issues.search",
            name="Search GitHub issues and pull requests",
            description="Search issues and pull requests in configured repositories.",
            kind=CapabilityKind.CONTEXT_READ,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "documents": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_id": {"type": "string", "maxLength": 128},
                                "title": {"type": "string", "maxLength": 160},
                                "text": {"type": "string", "maxLength": 2000},
                            },
                            "required": ["source_id", "title", "text"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["documents"],
                "additionalProperties": False,
            },
            timeout_seconds=20,
        )
        self._seed(
            self.plugins,
            "plugin_id",
            PluginDescriptor(
                plugin_id="github",
                plugin_type="github",
                display_name="GitHub",
                description="Read-only issue and pull request search.",
                capability_manifest=(github_search,),
                auth_schema={
                    "type": "object",
                    "properties": {"token": {"type": "string"}},
                    "required": ["token"],
                    "additionalProperties": False,
                },
                configuration_schema={
                    "type": "object",
                    "properties": {
                        "repositories": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 50,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
                                "maxLength": 100,
                            },
                        }
                    },
                    "required": ["repositories"],
                    "additionalProperties": False,
                },
                status=PluginDescriptorStatus.ACTIVE,
            ),
        )

    def _seed_slack_plugin(self) -> None:
        """Seed the built-in Slack plugin descriptor."""
        self._seed(
            self.plugins,
            "plugin_id",
            PluginDescriptor(
                plugin_id="slack",
                plugin_type="slack",
                display_name="Slack",
                description=(
                    "Read-only message search in configured channels, with optional "
                    "thread replies."
                ),
                capability_manifest=(
                    read_context_search(
                        "slack.messages.search",
                        "Search Slack messages",
                        (
                            "Search messages visible to the token in configured "
                            "channels; optionally include replies for the top "
                            "threaded match."
                        ),
                    ),
                ),
                auth_schema={
                    "type": "object",
                    "properties": {"user_token": {"type": "string"}},
                    "required": ["user_token"],
                    "additionalProperties": False,
                },
                configuration_schema={
                    "type": "object",
                    "properties": {
                        "channels": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 50,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "pattern": r"^[A-Za-z0-9_-]{1,80}$",
                            },
                        },
                        "include_thread_replies": {"type": "boolean"},
                    },
                    "required": ["channels"],
                    "additionalProperties": False,
                },
                status=PluginDescriptorStatus.ACTIVE,
            ),
        )

    def _seed_jira_plugin(
        self, jira_comment: CapabilitySpec, jira_search: CapabilitySpec
    ) -> None:
        """Seed the built-in Jira plugin descriptor."""
        self._seed(
            self.plugins,
            "plugin_id",
            PluginDescriptor(
                plugin_id="jira",
                plugin_type="jira_cloud",
                display_name="Jira Cloud",
                description="Read-only issue search scoped to configured projects.",
                capability_manifest=(jira_search, jira_comment),
                auth_schema={
                    "type": "object",
                    "properties": {"api_token": {"type": "string"}},
                    "required": ["api_token"],
                    "additionalProperties": False,
                },
                configuration_schema={
                    "type": "object",
                    "properties": {
                        "site_url": {
                            "type": "string",
                            "pattern": r"^https://[A-Za-z0-9-]+\.atlassian\.net/?$",
                        },
                        "email": {"type": "string", "format": "email"},
                        "project_keys": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 50,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "pattern": r"^[A-Z][A-Z0-9_]{0,49}$",
                            },
                        },
                    },
                    "required": ["site_url", "email", "project_keys"],
                    "additionalProperties": False,
                },
                status=PluginDescriptorStatus.ACTIVE,
            ),
        )

    def _seed_local_installation_and_policy(self, now: datetime) -> None:
        """Seed the local knowledge installation and approval policy."""
        self._seed(
            self.installations,
            "plugin_installation_id",
            PluginInstallation(
                plugin_installation_id="local-knowledge-installation",
                org_id="local-org",
                workspace_id="local-workspace",
                plugin_id="local-knowledge",
                display_name="Local knowledge",
                enabled_capability_ids=("knowledge.search",),
                status=PluginInstallationStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
        self._seed(
            self.approval_policies,
            "approval_policy_id",
            ApprovalPolicy(
                approval_policy_id="local-approval-policy",
                org_id="local-org",
                workspace_id="local-workspace",
                name="Require review for medium and high risk actions",
                required_risk_levels=(
                    RiskLevel.MEDIUM,
                    RiskLevel.HIGH,
                    RiskLevel.CRITICAL,
                ),
                allow_self_approval=True,
                enabled=True,
                created_at=now,
                updated_at=now,
            ),
        )
