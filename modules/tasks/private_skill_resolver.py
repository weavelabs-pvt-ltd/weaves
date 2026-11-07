"""Resolve owner-private skills into immutable snapshots for private tasks."""

from __future__ import annotations

from collections.abc import Callable

from application.ports.repository import Repository
from contracts.v1 import (
    AgentSkillSnapshot,
    CapabilityKind,
    Permission,
    PluginInstallation,
    SkillDefinition,
    SkillScope,
    SkillStatus,
    SkillVersion,
    TaskAudience,
)
from shared.request_context import effective_org_id, effective_workspace_id


class PrivateTaskSkillResolver:
    """Enforce private-task constraints and pin authorized skill versions."""

    def __init__(
        self,
        *,
        definitions: Repository[SkillDefinition],
        versions: Repository[SkillVersion],
        authorize: Callable[[str, Permission], None],
        capability_installation: Callable[..., tuple[PluginInstallation, object]],
    ) -> None:
        """Bind skill data and the capability policy used during task creation."""
        self._definitions = definitions
        self._versions = versions
        self._authorize = authorize
        self._capability_installation = capability_installation

    def resolve(
        self,
        skill_version_ids: tuple[str, ...],
        capability_ids: tuple[str, ...],
        *,
        owner: str,
        audience: TaskAudience,
        workflow_id: str | None,
        condition_only: bool,
    ) -> tuple[AgentSkillSnapshot, ...]:
        """Return immutable snapshots after owner and execution-policy checks."""
        private = audience is TaskAudience.PRIVATE
        if not private and skill_version_ids:
            raise ValueError("personal skills can only be used in private tasks")
        if len(skill_version_ids) > 5 or len(set(skill_version_ids)) != len(
            skill_version_ids
        ):
            raise ValueError("select at most five unique personal skill versions")
        if not skill_version_ids:
            return ()
        if workflow_id is not None or condition_only:
            raise ValueError("personal skills cannot run inside workflows")
        self._authorize(owner, Permission.SKILLS_USE)
        self._require_read_only_capabilities(capability_ids)
        definitions = self._definitions.list_scoped(effective_org_id())
        snapshots = [
            self._snapshot(version_id, owner, capability_ids, definitions)
            for version_id in skill_version_ids
        ]
        if self._context_size(snapshots) > 131_072:
            raise ValueError("selected personal skills exceed 128 KiB")
        return tuple(snapshots)

    def _require_read_only_capabilities(self, capability_ids: tuple[str, ...]) -> None:
        """Ensure private skills cannot grant write-capable agent tools."""
        for capability_id in capability_ids:
            self._capability_installation(
                capability_id,
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                require_enabled=True,
                allowed_kinds=(CapabilityKind.CONTEXT_READ,),
            )

    def _snapshot(
        self,
        version_id: str,
        owner: str,
        capability_ids: tuple[str, ...],
        definitions: tuple[SkillDefinition, ...],
    ) -> AgentSkillSnapshot:
        """Validate owner, lifecycle and required capabilities for one version."""
        version = self._versions.get_scoped(version_id, effective_org_id())
        definition = next(
            (item for item in definitions if item.skill_id == version.skill_id), None
        )
        if (
            definition is None
            or definition.scope is not SkillScope.PERSONAL
            or definition.owner_principal_id != owner
            or definition.status is not SkillStatus.ACTIVE
            or version.skill_id != definition.skill_id
        ):
            raise KeyError("personal skill version not found")
        if set(version.required_capability_ids) - set(capability_ids):
            raise ValueError("agent must allow all capabilities required by its skills")
        return AgentSkillSnapshot(
            skill_id=definition.skill_id,
            version=version.version,
            name=version.name,
            instructions=version.instructions,
            source_skill_version_id=version.skill_version_id,
            content_digest=version.content_digest,
            files=dict(version.files),
        )

    @staticmethod
    def _context_size(snapshots: list[AgentSkillSnapshot]) -> int:
        """Calculate encoded instruction and file size for pinned skills."""
        return sum(
            len(skill.instructions.encode("utf-8"))
            + sum(len(text.encode("utf-8")) for text in skill.files.values())
            for skill in snapshots
        )
