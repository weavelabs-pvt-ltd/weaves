"""Versioned skill catalog and portable package contracts."""

from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from contracts.v1.base import (
    ImmutableVersionContract,
    MutableOrgScopedContract,
    OpaqueId,
    StrEnum,
)
from contracts.v1.json_data import freeze_json_value, thaw_json_value


class SkillScope(StrEnum):
    """Visibility namespace for a skill definition."""

    PERSONAL = "personal"
    ORGANIZATION = "organization"


class SkillStatus(StrEnum):
    """Lifecycle state for a mutable skill definition."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class SkillDefinition(MutableOrgScopedContract):
    """Tenant-bound skill identity with an owner-private or org-visible scope."""

    skill_id: OpaqueId
    scope: SkillScope
    owner_principal_id: OpaqueId | None = None
    slug: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    current_version_id: OpaqueId
    status: SkillStatus = SkillStatus.ACTIVE
    maintainer_principal_ids: tuple[OpaqueId, ...] = ()

    @model_validator(mode="after")
    def scope_owner_matches(self) -> "SkillDefinition":
        """Require owners only for private definitions and unique maintainers."""
        if (self.scope is SkillScope.PERSONAL) != (self.owner_principal_id is not None):
            raise ValueError("personal skills require one owner; org skills do not")
        if len(self.maintainer_principal_ids) != len(
            set(self.maintainer_principal_ids)
        ):
            raise ValueError("skill maintainers must be unique")
        if self.scope is SkillScope.PERSONAL and self.maintainer_principal_ids:
            raise ValueError("personal skills cannot have additional maintainers")
        return self


class SkillVersion(ImmutableVersionContract):
    """Immutable, validated skill bundle version."""

    org_id: OpaqueId
    skill_version_id: OpaqueId
    skill_id: OpaqueId
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1, max_length=32768)
    files: dict[str, str] = Field(max_length=50)
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    required_capability_ids: tuple[str, ...] = Field(default=(), max_length=100)
    author_principal_id: OpaqueId

    @field_validator("files")
    @classmethod
    def files_are_immutable(cls, value: dict[str, str]) -> Any:
        """Freeze package contents after validating them as JSON text."""
        if any(
            not isinstance(path, str) or not isinstance(text, str)
            for path, text in value.items()
        ):
            raise ValueError("skill files must map paths to UTF-8 text")
        return freeze_json_value(value, "files")

    @field_serializer("files")
    def serialize_files(self, value: Any) -> Any:
        """Serialize immutable package content as a regular JSON object."""
        return thaw_json_value(value)


class SkillPublication(ImmutableVersionContract):
    """Self-review attestation and publication record for one exact version."""

    org_id: OpaqueId
    publication_id: OpaqueId
    skill_id: OpaqueId
    skill_version_id: OpaqueId
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    publisher_principal_id: OpaqueId
    self_review_confirmed: Literal[True]


class SkillRevocation(ImmutableVersionContract):
    """Immutable record that blocks one published release at runtime."""

    org_id: OpaqueId
    revocation_id: OpaqueId
    skill_id: OpaqueId
    skill_version_id: OpaqueId
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    revoked_by_principal_id: OpaqueId
    reason: str = Field(min_length=1, max_length=1000)
