"""Resolve what a request points at: the planner's model and Execute targets.

The planner runs on ``settings.planner_profile_id`` when that profile and its
provider are active; otherwise it falls back to the profile of the agent version
the task is pinned to (design decision 12), and with neither it cannot run (the
pipeline then fails closed). Repo and workflow targets only report facts (is the
repository covered by a GitHub installation, is the workflow published); whether
they are allowed is the engine's decision.
"""

from __future__ import annotations

from application.ports.repository import Repository
from contracts.v1 import (
    AgentVersion,
    ModelProfile,
    ModelProfileStatus,
    ModelProvider,
    ModelProviderStatus,
    OrganizationModelConfiguration,
    PluginInstallation,
    PluginInstallationStatus,
    Task,
    WorkflowDefinition,
)
from modules.slack.access.models import RepoTarget, WorkflowTarget
from modules.slack.pipeline.models import PlannerTarget


class PlannerTargets:
    """Pick the planner's model profile (own profile, else the agent's)."""

    def __init__(
        self,
        *,
        agent_versions: Repository[AgentVersion],
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProvider],
        model_configurations: Repository[OrganizationModelConfiguration],
    ) -> None:
        """Bind the model records of the organization."""
        self._versions = agent_versions
        self._profiles = model_profiles
        self._providers = providers
        self._configurations = model_configurations

    def resolve(
        self, planner_profile_id: str | None, task: Task
    ) -> PlannerTarget | None:
        """The planner's target for this task, or ``None`` when none is usable."""
        candidates = [planner_profile_id] if planner_profile_id else []
        try:
            version = self._versions.get_scoped(
                task.agent_version_id, task.org_id, task.workspace_id
            )
            candidates.append(version.model_profile_id)
        except KeyError:
            pass
        for profile_id in candidates:
            target = self._target(profile_id, task)
            if target is not None:
                return target
        return None

    def _target(self, profile_id: str, task: Task) -> PlannerTarget | None:
        try:
            profile = self._profiles.get_scoped(profile_id, task.org_id)
            provider = self._providers.get_scoped(profile.provider_id, task.org_id)
        except KeyError:
            return None
        if (
            profile.status is not ModelProfileStatus.ACTIVE
            or provider.status is not ModelProviderStatus.ACTIVE
        ):
            return None
        try:
            mode = self._configurations.get_scoped(task.org_id, task.org_id).mode
        except KeyError:
            mode = None
        return PlannerTarget(
            profile=profile,
            provider=provider,
            access_mode=mode,
            workspace_id=task.workspace_id,
            agent_id=task.agent_id,
        )


class ExecuteTargets:
    """Facts about a ``review`` or ``run`` target, never a decision."""

    def __init__(
        self,
        *,
        installations: Repository[PluginInstallation],
        workflows: Repository[WorkflowDefinition],
    ) -> None:
        """Bind the connector installations and the workflow definitions."""
        self._installations = installations
        self._workflows = workflows

    def repo(
        self, full_name: str | None, org_id: str, workspace_id: str
    ) -> RepoTarget | None:
        """The repository and whether an active GitHub installation covers it."""
        if not full_name:
            return None
        wanted = full_name.lower()
        covered = False
        for installation in self._installations.list_scoped(org_id, workspace_id):
            repos = installation.configuration.get("repositories")
            if (
                installation.plugin_id.startswith("github")
                and installation.status is PluginInstallationStatus.ACTIVE
                and isinstance(repos, (list, tuple))
                and wanted in {r.lower() for r in repos if isinstance(r, str)}
            ):
                covered = True
        return RepoTarget(wanted, covered)

    def workflow(
        self, workflow_id: str | None, org_id: str, workspace_id: str
    ) -> WorkflowTarget | None:
        """The workflow and whether it has a published version."""
        if not workflow_id:
            return None
        try:
            definition = self._workflows.get_scoped(workflow_id, org_id, workspace_id)
        except KeyError:
            return None
        return WorkflowTarget(workflow_id, definition.current_version_id is not None)
