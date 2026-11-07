"""Resolve and bound exact registry skill versions for agent releases."""

from collections.abc import Callable

from application.ports.repository import Repository
from contracts.v1 import (
    AgentSkillSnapshot,
    AgentVersion,
    Permission,
    SkillDefinition,
    SkillPublication,
    SkillRevocation,
    SkillScope,
    SkillStatus,
    SkillVersion,
)
from modules.agents.skills import snapshot_agent_skills
from shared.request_context import effective_org_id, effective_principal_id


class AgentSkillBindings:
    """Validate publication, ownership and capability rules for agent skills."""

    def __init__(
        self,
        *,
        definitions: Repository[SkillDefinition],
        versions: Repository[SkillVersion],
        publications: Repository[SkillPublication],
        revocations: Repository[SkillRevocation],
        authorize: Callable[[str, Permission], None],
    ) -> None:
        """Bind the registries and effective-permission check."""
        self._definitions = definitions
        self._versions = versions
        self._publications = publications
        self._revocations = revocations
        self._authorize = authorize

    def resolve_new(
        self,
        legacy_skill_ids: tuple[str, ...],
        skill_version_ids: tuple[str, ...],
        capability_ids: tuple[str, ...],
    ) -> tuple[AgentSkillSnapshot, ...]:
        """Resolve static aliases and explicit org releases for a new agent."""
        if legacy_skill_ids and skill_version_ids:
            raise ValueError("choose legacy skills or published skill versions")
        snapshots = snapshot_agent_skills(legacy_skill_ids) + self.snapshot_org_skills(
            skill_version_ids, capability_ids
        )
        self.validate_context(snapshots)
        return snapshots

    def snapshot_org_skills(
        self, skill_version_ids: tuple[str, ...], capability_ids: tuple[str, ...]
    ) -> tuple[AgentSkillSnapshot, ...]:
        """Resolve active, published and not-revoked org releases by version ID."""
        if not skill_version_ids:
            return ()
        if len(skill_version_ids) != len(set(skill_version_ids)):
            raise ValueError("published skill versions must be unique")
        actor = effective_principal_id("local-developer")
        self._authorize(actor, Permission.SKILLS_USE)
        org_id = effective_org_id()
        definitions = self._definitions.list_scoped(org_id)
        publications = self._publications.list_scoped(org_id)
        revocations = self._revocations.list_scoped(org_id)
        return tuple(
            self._snapshot_one(
                version_id,
                capability_ids,
                definitions,
                publications,
                revocations,
            )
            for version_id in skill_version_ids
        )

    def _snapshot_one(
        self,
        version_id: str,
        capability_ids: tuple[str, ...],
        definitions: tuple[SkillDefinition, ...],
        publications: tuple[SkillPublication, ...],
        revocations: tuple[SkillRevocation, ...],
    ) -> AgentSkillSnapshot:
        """Validate one org release and freeze it into an agent snapshot."""
        version = self._versions.get_scoped(version_id, effective_org_id())
        definition = next(
            (item for item in definitions if item.skill_id == version.skill_id), None
        )
        publication = next(
            (item for item in publications if item.skill_version_id == version_id),
            None,
        )
        revoked = any(item.skill_version_id == version_id for item in revocations)
        if (
            definition is None
            or definition.scope is not SkillScope.ORGANIZATION
            or definition.status is not SkillStatus.ACTIVE
            or publication is None
            or publication.content_digest != version.content_digest
            or revoked
        ):
            raise KeyError("published skill version not found")
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
    def validate_context(snapshots: tuple[AgentSkillSnapshot, ...]) -> None:
        """Bound selected skills and serialized procedure/reference bytes."""
        if len(snapshots) > 5:
            raise ValueError("agents may select at most five skills")
        context_size = sum(
            len(skill.instructions.encode("utf-8"))
            + sum(
                len(text.encode("utf-8"))
                for path, text in skill.files.items()
                if path != "SKILL.md"
            )
            for skill in snapshots
        )
        if context_size > 131_072:
            raise ValueError("selected skill context exceeds 128 KiB")

    def resolve_update(
        self,
        old_version: AgentVersion,
        legacy_skill_ids: tuple[str, ...] | None,
        skill_version_ids: tuple[str, ...] | None,
        capability_ids: tuple[str, ...],
    ) -> tuple[AgentSkillSnapshot, ...]:
        """Preserve or replace skill bindings on a newly published agent version."""
        if skill_version_ids is not None:
            if legacy_skill_ids is not None:
                raise ValueError("choose legacy skills or published skill versions")
            snapshots = self.snapshot_org_skills(skill_version_ids, capability_ids)
        elif legacy_skill_ids is not None:
            snapshots = snapshot_agent_skills(legacy_skill_ids)
        else:
            snapshots = old_version.skills
        self.validate_context(snapshots)
        return snapshots
