"""The review agent, and the switch that lets the GitHub connection read pull requests.

``ReviewProvisioner`` keeps one agent per organization ("Slack PR review"): the
review instructions, one capability (the pull request reader), the strict output
schema and the review budget, on the organization's default model profile. It is
created lazily on the first allowed review, as the installing administrator (the
tenant identity the dispatcher already runs under), inside the organization's
Slack mutation lock, and is refreshed when the instructions, schema, capability
or a dead model profile change. A review task itself is created as the mapped
member, so RBAC and audit follow the member.

``ReviewEnabler`` turns the reader on for the one GitHub connection that covers
the repository, the first time an admin-allowlisted repository is reviewed (GitHub
connections made before this slice only list the search capabilities). The
pre-flight has already passed when it runs; the change is audited.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from application.ports.repository import Repository
from contracts.v1 import (
    AgentBudget,
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    ModelProfile,
    ModelProfileStatus,
    PluginInstallation,
    PluginInstallationStatus,
)
from contracts.v1.json_data import thaw_json_value
from modules.integrations.github_review_spec import PR_REVIEW_CAPABILITY_ID
from modules.slack.review.prompt import (
    REVIEW_AGENT_NAME,
    REVIEW_BUDGET,
    REVIEW_INSTRUCTIONS,
    REVIEW_OUTPUT_SCHEMA,
    REVIEW_PERSONA,
)
from shared.clock import new_id, utc_now

logger = logging.getLogger("product.slack")
DESCRIPTION = "Reviews pull requests asked for in Slack. Managed by Weaves."
CAPABILITIES = (PR_REVIEW_CAPABILITY_ID,)


class ReviewUnavailable(Exception):
    """The review agent cannot be set up (the caller says "unavailable")."""


class NoReviewModel(Exception):
    """The organization has no active model profile to review with."""


class AgentWriter(Protocol):
    """The two agent operations provisioning needs (the real agent service)."""

    def create_agent(
        self,
        *,
        name: str,
        instructions: str,
        persona: str = ...,
        description: str = ...,
        model_profile_id: str | None = ...,
        allowed_capability_ids: tuple[str, ...] = ...,
        budget: AgentBudget | None = ...,
        output_schema: dict[str, Any] | None = ...,
    ) -> AgentDefinition:
        """Create an agent with its first version."""
        ...

    def update_agent(
        self,
        agent_id: str,
        *,
        instructions: str | None = ...,
        persona: str | None = ...,
        model_profile_id: str | None = ...,
        allowed_capability_ids: tuple[str, ...] | None = ...,
        budget: AgentBudget | None = ...,
        output_schema: dict[str, Any] | None = ...,
    ) -> AgentDefinition:
        """Publish a new version with the changed fields."""
        ...


class ReviewProvisioner:
    """Create the review agent once and keep it current."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_versions: Repository[AgentVersion],
        agent_service: AgentWriter,
        model_profiles: Repository[ModelProfile],
        default_profile: Callable[[], ModelProfile],
        mutation: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind the agent records, the agent service and the Slack mutation lock."""
        self._agents = agents
        self._versions = agent_versions
        self._service = agent_service
        self._profiles = model_profiles
        self._default_profile = default_profile
        self._mutation = mutation

    def ensure(self, org_id: str, workspace_id: str) -> str:
        """The id of the usable review agent, created or refreshed as needed."""
        try:
            profile = self._default_profile()
        except (ValueError, KeyError):
            raise NoReviewModel() from None
        with self._mutation():
            agent = self._find(org_id, workspace_id)
            try:
                if agent is None:
                    return self._create(profile)
                self._refresh(agent, profile, org_id)
            except Exception as error:
                logger.warning(
                    "slack_review_agent_failed kind=%s", type(error).__name__
                )
                raise ReviewUnavailable() from None
            return agent.id

    def _find(self, org_id: str, workspace_id: str) -> AgentDefinition | None:
        for agent in self._agents.list_scoped(org_id, workspace_id):
            if agent.name == REVIEW_AGENT_NAME and agent.status is AgentStatus.ACTIVE:
                return agent
        return None

    def _create(self, profile: ModelProfile) -> str:
        agent = self._service.create_agent(
            name=REVIEW_AGENT_NAME,
            description=DESCRIPTION,
            persona=REVIEW_PERSONA,
            instructions=REVIEW_INSTRUCTIONS,
            model_profile_id=profile.id,
            allowed_capability_ids=CAPABILITIES,
            budget=REVIEW_BUDGET,
            output_schema=REVIEW_OUTPUT_SCHEMA,
        )
        return agent.id

    def _refresh(
        self, agent: AgentDefinition, profile: ModelProfile, org_id: str
    ) -> None:
        current = self._versions.get(agent.current_version_id or "")
        dead = self._profile_unusable(current.model_profile_id, org_id)
        if not dead and _is_current(current):
            return
        self._service.update_agent(
            agent.id,
            instructions=REVIEW_INSTRUCTIONS,
            persona=REVIEW_PERSONA,
            allowed_capability_ids=CAPABILITIES,
            budget=REVIEW_BUDGET,
            output_schema=REVIEW_OUTPUT_SCHEMA,
            model_profile_id=profile.id if dead else None,
        )

    def _profile_unusable(self, profile_id: str, org_id: str) -> bool:
        try:
            profile = self._profiles.get_scoped(profile_id, org_id)
        except KeyError:
            return True
        return profile.status is not ModelProfileStatus.ACTIVE


def _is_current(version: AgentVersion) -> bool:
    schema = (
        thaw_json_value(version.output_schema)
        if version.output_schema is not None
        else None
    )
    return (
        version.instructions == REVIEW_INSTRUCTIONS
        and version.persona == REVIEW_PERSONA
        and tuple(version.allowed_capability_ids) == CAPABILITIES
        and version.budget == REVIEW_BUDGET
        and schema == REVIEW_OUTPUT_SCHEMA
    )


class ReviewEnabler:
    """Enable the pull request reader on the GitHub connection covering a repository."""

    def __init__(
        self,
        *,
        installations: Repository[PluginInstallation],
        audit_events: Repository[AuditEvent],
    ) -> None:
        """Bind the connector installations and the audit trail."""
        self._installations = installations
        self._audit = audit_events

    def ensure(
        self, org_id: str, workspace_id: str, full_name: str, request_id: str
    ) -> bool:
        """``True`` when exactly one covering connection can read pull requests now."""
        covering = sorted(
            (
                item
                for item in self._installations.list_scoped(org_id, workspace_id)
                if item.plugin_id.startswith("github")
                and item.status is PluginInstallationStatus.ACTIVE
                and full_name in _repositories(item)
            ),
            key=lambda item: item.plugin_installation_id,
        )
        if not covering:
            return False
        enabled = [
            i for i in covering if PR_REVIEW_CAPABILITY_ID in i.enabled_capability_ids
        ]
        if enabled:
            return True
        chosen = covering[0]
        updated = PluginInstallation.model_validate(
            chosen.model_copy(
                update={
                    "enabled_capability_ids": (
                        *chosen.enabled_capability_ids,
                        PR_REVIEW_CAPABILITY_ID,
                    ),
                    "updated_at": utc_now(),
                }
            ).model_dump()
        )
        self._installations.put(updated)
        self._audit.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id="system:slack",
                action="slack.review.capability_enabled",
                target_type="plugin_installation",
                target_id=chosen.plugin_installation_id,
                summary="Pull request reading enabled for Slack reviews",
                request_id=request_id,
                metadata={"capability": PR_REVIEW_CAPABILITY_ID},
                created_at=utc_now(),
            )
        )
        return True


def _repositories(installation: PluginInstallation) -> set[str]:
    """The lower-case repositories a GitHub connection is configured with."""
    listed = installation.configuration.get("repositories")
    if not isinstance(listed, (list, tuple)):
        return set()
    return {item.lower() for item in listed if isinstance(item, str)}
