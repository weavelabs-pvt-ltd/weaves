"""Author private skills and publish reviewed snapshots to an organization."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import cast

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Organization,
    OrganizationStatus,
    Principal,
    SkillDefinition,
    SkillPublication,
    SkillRevocation,
    SkillScope,
    SkillStatus,
    SkillVersion,
    User,
)
from contracts.v1.identity import Permission
from modules.skills.errors import SkillRevisionConflict
from modules.skills.lifecycle import SkillLifecycleMixin
from modules.skills.package_validation import validate_skill_package
from shared.clock import new_id, utc_now
from shared.request_context import effective_org_id


class SkillsService(SkillLifecycleMixin):
    """Enforce author ownership, tenant visibility and explicit publication."""

    def __init__(
        self,
        *,
        definitions: Repository[SkillDefinition],
        versions: Repository[SkillVersion],
        publications: Repository[SkillPublication],
        revocations: Repository[SkillRevocation],
        principals: Repository[Principal],
        users: Repository[User],
        organizations: Repository[Organization],
        audit_events: Repository[AuditEvent],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        advisory_transaction_lock: Callable[[str], None] | None = None,
    ) -> None:
        """Bind repositories and authorization/transaction boundaries."""
        self._definitions = definitions
        self._versions = versions
        self._publications = publications
        self._revocations = revocations
        self._principals = principals
        self._users = users
        self._organizations = organizations
        self._audit_events = audit_events
        self._authorize = authorize
        self._unit_of_work = unit_of_work
        self._advisory_transaction_lock = advisory_transaction_lock

    def list_visible(self) -> tuple[tuple[SkillDefinition, SkillVersion], ...]:
        """List published organization skills and the caller's private skills."""
        org_id = effective_org_id()
        actor = self._actor(Permission.SKILLS_VIEW)
        records = self._definitions.list_scoped(org_id)
        visible = [
            definition
            for definition in records
            if definition.status is SkillStatus.ACTIVE
            and (
                definition.scope is SkillScope.ORGANIZATION
                or definition.owner_principal_id == actor
            )
        ]
        return tuple(
            (
                definition,
                self._versions.get_scoped(definition.current_version_id, org_id),
            )
            for definition in visible
        )

    def get_version(self, skill_version_id: str) -> SkillVersion:
        """Read a version only when its definition is visible to this member."""
        actor = self._actor(Permission.SKILLS_VIEW)
        version = self._versions.get_scoped(skill_version_id, effective_org_id())
        definition = self._definitions.get_scoped(version.skill_id, effective_org_id())
        if (
            definition.scope is SkillScope.PERSONAL
            and definition.owner_principal_id != actor
        ):
            raise KeyError("skill version not found")
        return version

    def author_display_names(
        self, versions: tuple[SkillVersion, ...]
    ) -> dict[str, str]:
        """Resolve visible skill authors without exposing the member directory."""
        self._actor(Permission.SKILLS_VIEW)
        names: dict[str, str] = {}
        for author in {version.author_principal_id for version in versions}:
            try:
                principal = self._principals.get_scoped(author, effective_org_id())
                user = self._users.get_scoped(
                    principal.user_id or "", effective_org_id()
                )
                names[author] = user.display_name
            except KeyError:
                names[author] = "Organization member"
        return names

    def create_personal_skill(
        self,
        files: dict[str, str],
        required_capability_ids: tuple[str, ...] = (),
    ) -> SkillDefinition:
        """Create a private skill and seal its first immutable version."""
        actor = self._actor(Permission.SKILLS_PERSONAL_MANAGE)
        package = validate_skill_package(files, required_capability_ids)
        org_id = effective_org_id()
        now = utc_now()
        skill_id, version_id = new_id(), new_id()
        definition = SkillDefinition(
            skill_id=skill_id,
            org_id=org_id,
            scope=SkillScope.PERSONAL,
            owner_principal_id=actor,
            slug=package["name"],
            current_version_id=version_id,
            created_at=now,
            updated_at=now,
        )
        version = self._version_from_package(
            definition, package, version_id=version_id, version_number=1, actor=actor
        )
        with self._unit_of_work():
            self._lock(
                f"skill-slug:{effective_org_id()}:personal:{actor}:{package['name']}"
            )
            self._ensure_slug_available(package["name"], owner=actor)
            self._definitions.create(definition)
            self._versions.create(version)
            self._audit(actor, "skill.personal.created", definition, version)
        return definition

    def save_personal_version(
        self,
        skill_id: str,
        files: dict[str, str],
        *,
        expected_current_version_id: str,
        required_capability_ids: tuple[str, ...] = (),
    ) -> SkillDefinition:
        """Save a new immutable private version owned by the caller."""
        actor = self._actor(Permission.SKILLS_PERSONAL_MANAGE)
        definition = self._get_owned_personal(skill_id, actor)
        if definition.status is not SkillStatus.ACTIVE:
            raise ValueError("restore the archived skill before editing it")
        package = validate_skill_package(files, required_capability_ids)
        if package["name"] != definition.slug:
            raise ValueError("skill slug cannot change after creation")
        now = utc_now()
        version_id = new_id()
        with self._unit_of_work():
            self._lock(f"skill-version:{effective_org_id()}:{skill_id}")
            definition = self._get_owned_personal(skill_id, actor)
            if definition.current_version_id != expected_current_version_id:
                raise SkillRevisionConflict(
                    "skill changed since it was loaded; refresh before saving"
                )
            versions = self._versions.list_scoped(effective_org_id())
            next_version = (
                max(
                    (item.version for item in versions if item.skill_id == skill_id),
                    default=0,
                )
                + 1
            )
            version = self._version_from_package(
                definition,
                package,
                version_id=version_id,
                version_number=next_version,
                actor=actor,
            )
            updated = definition.model_copy(
                update={"current_version_id": version_id, "updated_at": now}
            )
            self._versions.create(version)
            self._definitions.put(updated)
            self._audit(actor, "skill.personal.version_saved", updated, version)
        return updated

    def publish_personal_skill(
        self,
        skill_id: str,
        skill_version_id: str,
        content_digest: str,
        *,
        self_review_confirmed: bool,
        expected_org_current_version_id: str | None = None,
    ) -> SkillDefinition:
        """Copy an exact owner-reviewed private version into the org catalog."""
        actor = self._actor(Permission.SKILLS_ORG_PUBLISH)
        if not self_review_confirmed:
            raise ValueError("confirm review of this exact version for org visibility")
        personal, candidate = self._reviewed_candidate(
            skill_id, skill_version_id, content_digest, actor
        )
        organization = self._organizations.get(effective_org_id())
        if organization.status is not OrganizationStatus.ACTIVE:
            raise PermissionError(
                "skills cannot be published to an inactive organization"
            )
        existing = self._find_org_skill(candidate.name)
        if existing is not None:
            return self._publish_org_revision(
                actor,
                personal,
                candidate,
                existing,
                content_digest,
                expected_org_current_version_id,
            )
        if expected_org_current_version_id is not None:
            raise ValueError("no organization release exists for the supplied revision")
        return self._publish_new_org_skill(actor, personal, candidate, content_digest)

    def _reviewed_candidate(
        self, skill_id: str, version_id: str, digest: str, actor: str
    ) -> tuple[SkillDefinition, SkillVersion]:
        """Validate ownership and prove the reviewed immutable version is current."""
        personal = self._get_owned_personal(skill_id, actor)
        if personal.status is not SkillStatus.ACTIVE:
            raise ValueError("restore the archived skill before publishing it")
        candidate = self._versions.get_scoped(version_id, effective_org_id())
        if candidate.skill_id != skill_id or candidate.content_digest != digest:
            raise ValueError("reviewed skill version or content digest does not match")
        if candidate.skill_version_id != personal.current_version_id:
            raise ValueError(
                "publish the current saved version; save a new version first"
            )
        return personal, candidate

    def _publish_new_org_skill(
        self,
        actor: str,
        personal: SkillDefinition,
        candidate: SkillVersion,
        digest: str,
    ) -> SkillDefinition:
        """Create the first organization release from a reviewed private skill."""
        self._ensure_slug_available(candidate.name, owner=None)
        now, org_skill_id, org_version_id = utc_now(), new_id(), new_id()
        definition = SkillDefinition(
            skill_id=org_skill_id,
            org_id=effective_org_id(),
            scope=SkillScope.ORGANIZATION,
            owner_principal_id=None,
            slug=candidate.name,
            current_version_id=org_version_id,
            maintainer_principal_ids=(actor,),
            created_at=now,
            updated_at=now,
        )
        version = candidate.model_copy(
            update={
                "skill_id": org_skill_id,
                "skill_version_id": org_version_id,
                "version": 1,
                "author_principal_id": actor,
                "created_at": now,
            }
        )
        publication = SkillPublication(
            org_id=effective_org_id(),
            publication_id=new_id(),
            skill_id=org_skill_id,
            skill_version_id=org_version_id,
            content_digest=digest,
            publisher_principal_id=actor,
            self_review_confirmed=True,
            created_at=now,
        )
        self._persist_new_org_release(
            actor, personal, candidate, definition, version, publication
        )
        return definition

    def _persist_new_org_release(
        self,
        actor: str,
        personal: SkillDefinition,
        candidate: SkillVersion,
        definition: SkillDefinition,
        version: SkillVersion,
        publication: SkillPublication,
    ) -> None:
        """Recheck the reviewed source under lock and persist the release."""
        with self._unit_of_work():
            self._lock(f"skill-slug:{effective_org_id()}:organization:{candidate.name}")
            current_personal = self._get_owned_personal(personal.skill_id, actor)
            current_candidate = self._versions.get_scoped(
                candidate.skill_version_id, effective_org_id()
            )
            if (
                current_personal.current_version_id != candidate.skill_version_id
                or current_candidate.content_digest != publication.content_digest
            ):
                raise ValueError(
                    "skill changed after self-review; review the latest version"
                )
            if (
                self._organizations.get(effective_org_id()).status
                is not OrganizationStatus.ACTIVE
            ):
                raise PermissionError(
                    "skills cannot be published to an inactive organization"
                )
            self._ensure_slug_available(candidate.name, owner=None)
            self._definitions.create(definition)
            self._versions.create(version)
            self._publications.create(publication)
            self._audit(actor, "skill.organization.published", definition, version)

    def _publish_org_revision(
        self,
        actor: str,
        personal: SkillDefinition,
        candidate: SkillVersion,
        current: SkillDefinition,
        content_digest: str,
        expected_current_version_id: str | None,
    ) -> SkillDefinition:
        """Publish a reviewed revision when the caller maintains the org skill."""
        if actor not in current.maintainer_principal_ids:
            self._authorize(actor, Permission.SKILLS_ORG_MANAGE)
        if expected_current_version_id is None:
            raise SkillRevisionConflict(
                "include the current organization version to publish an update"
            )
        if current.current_version_id != expected_current_version_id:
            raise SkillRevisionConflict(
                "organization skill changed; refresh and review the current release"
            )
        now, version_id = utc_now(), new_id()
        with self._unit_of_work():
            self._lock(f"skill-version:{effective_org_id()}:{current.skill_id}")
            current = self._definitions.get_scoped(current.skill_id, effective_org_id())
            personal = self._get_owned_personal(personal.skill_id, actor)
            candidate = self._versions.get_scoped(
                candidate.skill_version_id, effective_org_id()
            )
            if (
                current.current_version_id != expected_current_version_id
                or personal.current_version_id != candidate.skill_version_id
                or candidate.content_digest != content_digest
            ):
                raise SkillRevisionConflict(
                    "skill changed after self-review; review the latest version"
                )
            versions = self._versions.list_scoped(effective_org_id())
            next_number = (
                max(
                    (
                        item.version
                        for item in versions
                        if item.skill_id == current.skill_id
                    ),
                    default=0,
                )
                + 1
            )
            published = candidate.model_copy(
                update={
                    "skill_id": current.skill_id,
                    "skill_version_id": version_id,
                    "version": next_number,
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
                skill_id=current.skill_id,
                skill_version_id=version_id,
                content_digest=content_digest,
                publisher_principal_id=actor,
                self_review_confirmed=True,
                created_at=now,
            )
            self._versions.create(published)
            self._definitions.put(updated)
            self._publications.create(publication)
            self._audit(actor, "skill.organization.revised", updated, published)
        return updated

    def _find_org_skill(self, slug: str) -> SkillDefinition | None:
        """Find an active organization definition by its tenant-unique slug."""
        return next(
            (
                item
                for item in self._definitions.list_scoped(effective_org_id())
                if item.scope is SkillScope.ORGANIZATION
                and item.status is SkillStatus.ACTIVE
                and item.slug == slug
            ),
            None,
        )

    def _ensure_slug_available(self, slug: str, *, owner: str | None) -> None:
        """Prevent collisions within the org or caller-private namespace."""
        for definition in self._definitions.list_scoped(effective_org_id()):
            same_namespace = (
                definition.scope is SkillScope.ORGANIZATION
                if owner is None
                else definition.scope is SkillScope.PERSONAL
                and definition.owner_principal_id == owner
            )
            if same_namespace and definition.slug == slug:
                raise ValueError(
                    "a skill with this slug already exists in the target catalog"
                )

    def _version_from_package(
        self,
        definition: SkillDefinition,
        package: dict[str, object],
        *,
        version_id: str,
        version_number: int,
        actor: str,
    ) -> SkillVersion:
        """Map validated package data to an immutable version record."""
        return SkillVersion(
            org_id=definition.org_id,
            skill_version_id=version_id,
            skill_id=definition.skill_id,
            version=version_number,
            name=str(package["name"]),
            description=str(package["description"]),
            instructions=str(package["instructions"]),
            files=cast(dict[str, str], package["files"]),
            content_digest=str(package["content_digest"]),
            required_capability_ids=cast(
                tuple[str, ...], package["required_capability_ids"]
            ),
            author_principal_id=actor,
            created_at=utc_now(),
        )

    def _audit(
        self,
        actor: str,
        action: str,
        definition: SkillDefinition,
        version: SkillVersion,
    ) -> None:
        """Record privacy-safe skill lifecycle metadata."""
        if definition.scope is SkillScope.PERSONAL:
            return
        self._audit_events.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=definition.org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type="skill",
                target_id=definition.skill_id,
                summary="Skill package lifecycle change",
                request_id=new_id(),
                metadata={
                    "version_id": version.skill_version_id,
                    "content_digest": version.content_digest,
                },
                created_at=utc_now(),
            )
        )

    def _lock(self, key: str) -> None:
        """Serialize a skill namespace or version counter in PostgreSQL."""
        if self._advisory_transaction_lock is not None:
            self._advisory_transaction_lock(key)
