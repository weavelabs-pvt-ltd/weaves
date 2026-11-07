"""Lifecycle operations for immutable skill releases and visibility."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Organization,
    Permission,
    SkillDefinition,
    SkillPublication,
    SkillRevocation,
    SkillScope,
    SkillStatus,
    SkillVersion,
)
from contracts.v1.identity import Principal, User
from modules.skills.errors import SkillRevisionConflict
from shared.clock import new_id, utc_now
from shared.request_context import effective_org_id, effective_principal_id


class SkillLifecycleMixin:
    """Add immutable history, archive, restore and emergency revoke controls."""

    _definitions: Repository[SkillDefinition]
    _versions: Repository[SkillVersion]
    _publications: Repository[SkillPublication]
    _revocations: Repository[SkillRevocation]
    _principals: Repository[Principal]
    _users: Repository[User]
    _organizations: Repository[Organization]
    _audit_events: Repository[AuditEvent]
    _authorize: Callable[[str, Permission], None]
    _unit_of_work: Callable[[], AbstractContextManager[None]]

    def _actor(self, permission: Permission) -> str:
        """Authorize an active human member for one lifecycle operation."""
        actor = effective_principal_id("local-developer")
        self._authorize(actor, permission)
        principal = self._principals.get_scoped(actor, effective_org_id())
        if (
            principal.status.value != "active"
            or principal.principal_type.value != "user"
        ):
            raise PermissionError("skills are available only to active human members")
        user = self._users.get_scoped(principal.user_id or "", effective_org_id())
        if user.status.value != "active":
            raise PermissionError("skills are available only to active human members")
        return actor

    def _get_owned_personal(self, skill_id: str, actor: str) -> SkillDefinition:
        """Return a caller-owned private skill without revealing other IDs."""
        try:
            definition = self._definitions.get_scoped(skill_id, effective_org_id())
        except KeyError:
            raise KeyError("skill not found") from None
        if (
            definition.scope is not SkillScope.PERSONAL
            or definition.owner_principal_id != actor
        ):
            raise KeyError("skill not found")
        return definition

    def _visible_definition(self, skill_id: str, actor: str) -> SkillDefinition:
        """Resolve an organization or caller-private definition by visibility."""
        try:
            definition = self._definitions.get_scoped(skill_id, effective_org_id())
        except KeyError:
            raise KeyError("skill not found") from None
        if (
            definition.scope is SkillScope.PERSONAL
            and definition.owner_principal_id != actor
        ):
            raise KeyError("skill not found")
        return definition

    def list_versions(self, skill_id: str) -> tuple[SkillVersion, ...]:
        """Return immutable history for a skill visible to the caller."""
        actor = self._actor(Permission.SKILLS_VIEW)
        definition = self._visible_definition(skill_id, actor)
        return tuple(
            sorted(
                (
                    version
                    for version in self._versions.list_scoped(effective_org_id())
                    if version.skill_id == definition.skill_id
                ),
                key=lambda item: item.version,
                reverse=True,
            )
        )

    def validate_package(
        self, files: dict[str, str], required_capability_ids: tuple[str, ...] = ()
    ) -> dict[str, object]:
        """Validate package metadata without persisting or executing the draft."""
        from modules.skills.package_validation import validate_skill_package

        self._actor(Permission.SKILLS_PERSONAL_MANAGE)
        package = validate_skill_package(files, required_capability_ids)
        return {
            "name": package["name"],
            "description": package["description"],
            "content_digest": package["content_digest"],
            "required_capability_ids": package["required_capability_ids"],
            "file_count": len(package["files"]),
        }

    def set_archived(self, skill_id: str, *, archived: bool) -> SkillDefinition:
        """Hide or restore a skill without changing immutable releases."""
        actor = effective_principal_id("local-developer")
        definition = self._definitions.get_scoped(skill_id, effective_org_id())
        if definition.scope is SkillScope.PERSONAL:
            actor = self._actor(Permission.SKILLS_PERSONAL_MANAGE)
            definition = self._get_owned_personal(skill_id, actor)
        else:
            actor = self._actor(Permission.SKILLS_ORG_PUBLISH)
            if actor not in definition.maintainer_principal_ids:
                self._authorize(actor, Permission.SKILLS_ORG_MANAGE)
        target = SkillStatus.ARCHIVED if archived else SkillStatus.ACTIVE
        if definition.status is target:
            return definition
        updated = definition.model_copy(
            update={"status": target, "updated_at": utc_now()}
        )
        with self._unit_of_work():
            self._lock(f"skill-version:{effective_org_id()}:{skill_id}")
            current = self._definitions.get_scoped(skill_id, effective_org_id())
            if current.updated_at != definition.updated_at:
                raise SkillRevisionConflict("skill changed; refresh before retrying")
            saved = self._definitions.put(updated)
            self._audit_lifecycle(
                actor,
                "skill.archived" if archived else "skill.restored",
                saved,
            )
        return saved

    def revoke_release(self, skill_version_id: str, reason: str) -> SkillRevocation:
        """Permanently block a published release at runtime boundaries."""
        actor = self._actor(Permission.SKILLS_ORG_PUBLISH)
        version = self._versions.get_scoped(skill_version_id, effective_org_id())
        definition = self._definitions.get_scoped(version.skill_id, effective_org_id())
        if definition.scope is not SkillScope.ORGANIZATION:
            raise KeyError("published skill release not found")
        if actor not in definition.maintainer_principal_ids:
            self._authorize(actor, Permission.SKILLS_REVOKE)
        if not any(
            item.skill_version_id == skill_version_id
            and item.content_digest == version.content_digest
            for item in self._publications.list_scoped(effective_org_id())
        ):
            raise KeyError("published skill release not found")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("a revocation reason is required")
        existing = self._revocation_for_version(skill_version_id)
        if existing is not None:
            return existing
        revocation = SkillRevocation(
            org_id=effective_org_id(),
            revocation_id=new_id(),
            skill_id=definition.skill_id,
            skill_version_id=version.skill_version_id,
            content_digest=version.content_digest,
            revoked_by_principal_id=actor,
            reason=clean_reason,
            created_at=utc_now(),
        )
        with self._unit_of_work():
            self._lock(f"skill-revocation:{effective_org_id()}:{skill_version_id}")
            current = self._revocation_for_version(skill_version_id)
            if current is not None:
                return current
            self._revocations.create(revocation)
            self._audit_lifecycle(
                actor,
                "skill.release.revoked",
                definition,
                version,
                extra_metadata={"reason": clean_reason},
            )
        return revocation

    def restore_release(
        self,
        skill_id: str,
        source_version_id: str,
        *,
        expected_current_version_id: str,
        self_review_confirmed: bool,
    ) -> tuple[SkillDefinition, SkillVersion]:
        """Republish old content as a newly reviewed immutable release."""
        actor = self._actor(Permission.SKILLS_ORG_PUBLISH)
        if not self_review_confirmed:
            raise ValueError("confirm review of the exact release before restoring it")
        definition = self._definitions.get_scoped(skill_id, effective_org_id())
        if definition.scope is not SkillScope.ORGANIZATION:
            raise KeyError("organization skill not found")
        if definition.status is not SkillStatus.ACTIVE:
            raise ValueError("unarchive the skill before restoring a release")
        if actor not in definition.maintainer_principal_ids:
            self._authorize(actor, Permission.SKILLS_ORG_MANAGE)
        if definition.current_version_id != expected_current_version_id:
            raise SkillRevisionConflict("organization skill changed; refresh first")
        source = self._published_source(skill_id, source_version_id)
        with self._unit_of_work():
            self._lock(f"skill-version:{effective_org_id()}:{skill_id}")
            current = self._definitions.get_scoped(skill_id, effective_org_id())
            if current.current_version_id != expected_current_version_id:
                raise SkillRevisionConflict("organization skill changed; refresh first")
            number = (
                max((item.version for item in self.list_versions(skill_id)), default=0)
                + 1
            )
            now, version_id = utc_now(), new_id()
            restored = source.model_copy(
                update={
                    "skill_version_id": version_id,
                    "version": number,
                    "author_principal_id": actor,
                    "created_at": now,
                }
            )
            updated = current.model_copy(
                update={"current_version_id": version_id, "updated_at": now}
            )
            publication = SkillPublication(
                org_id=effective_org_id(),
                publication_id=new_id(),
                skill_id=skill_id,
                skill_version_id=version_id,
                content_digest=source.content_digest,
                publisher_principal_id=actor,
                self_review_confirmed=True,
                created_at=now,
            )
            self._versions.create(restored)
            self._definitions.put(updated)
            self._publications.create(publication)
            self._audit(actor, "skill.organization.restored", updated, restored)
        return updated, restored

    def _revocation_for_version(self, version_id: str) -> SkillRevocation | None:
        """Find the revocation record for a release in the active organization."""
        return next(
            (
                item
                for item in self._revocations.list_scoped(effective_org_id())
                if item.skill_version_id == version_id
            ),
            None,
        )

    def _published_source(self, skill_id: str, version_id: str) -> SkillVersion:
        """Resolve an exact formerly published release owned by this definition."""
        source = self._versions.get_scoped(version_id, effective_org_id())
        if source.skill_id != skill_id:
            raise KeyError("source release not found")
        if not any(
            item.skill_version_id == version_id
            and item.content_digest == source.content_digest
            for item in self._publications.list_scoped(effective_org_id())
        ):
            raise ValueError("only previously published releases can be restored")
        return source

    def _audit_lifecycle(
        self,
        actor: str,
        action: str,
        definition: SkillDefinition,
        version: SkillVersion | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> None:
        """Record org lifecycle metadata without storing package content."""
        if definition.scope is SkillScope.PERSONAL:
            return
        metadata = (
            {
                "version_id": version.skill_version_id,
                "content_digest": version.content_digest,
            }
            if version is not None
            else {"status": definition.status.value}
        )
        metadata.update(extra_metadata or {})
        self._audit_events.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=definition.org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type="skill",
                target_id=definition.skill_id,
                summary="Skill lifecycle change",
                request_id=new_id(),
                metadata=metadata,
                created_at=utc_now(),
            )
        )

    def _lock(self, key: str) -> None:
        """Acquire the optional transaction-scoped lifecycle lock."""
        raise NotImplementedError

    def _audit(
        self,
        actor: str,
        action: str,
        definition: SkillDefinition,
        version: SkillVersion,
    ) -> None:
        """Write the organization-visible audit for a release lifecycle action."""
        raise NotImplementedError
