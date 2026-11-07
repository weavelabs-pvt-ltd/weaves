"""Skills catalog and authoring HTTP endpoints."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from modules.skills.errors import SkillRevisionConflict
from modules.skills.service import SkillsService


class SkillPackageRequest(BaseModel):
    """Portable text files submitted for private skill authoring."""

    model_config = ConfigDict(extra="forbid")

    files: dict[str, str] = Field(min_length=1, max_length=50)
    required_capability_ids: tuple[str, ...] = Field(default=(), max_length=100)


class SkillPublishRequest(BaseModel):
    """Exact candidate and explicit content-visibility confirmation."""

    model_config = ConfigDict(extra="forbid")

    skill_version_id: str = Field(min_length=1, max_length=128)
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    self_review_confirmed: bool
    expected_org_current_version_id: str | None = Field(default=None, max_length=128)


class SkillReleaseRestoreRequest(BaseModel):
    """Reviewed source release and concurrency guard for a restore action."""

    model_config = ConfigDict(extra="forbid")

    source_version_id: str = Field(min_length=1, max_length=128)
    expected_current_version_id: str = Field(min_length=1, max_length=128)
    self_review_confirmed: bool


class SkillRevocationRequest(BaseModel):
    """Human reason for permanently blocking a published version."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1000)


def register_skill_routes(
    app: FastAPI,
    service: SkillsService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register private authoring and organization catalog endpoints."""
    handlers = SkillRouteHandlers(service, dump)
    app.get("/api/v0/skills")(handlers.list_skills)
    app.post("/api/v0/skills/validate")(handlers.validate_package)
    app.get("/api/v0/skills/versions/{version_id}")(handlers.get_version)
    app.post("/api/v0/skills/personal", status_code=201)(handlers.create_personal_skill)
    app.get("/api/v0/skills/{skill_id}/versions")(handlers.list_versions)
    app.post("/api/v0/skills/{skill_id}/archive")(handlers.archive_skill)
    app.post("/api/v0/skills/{skill_id}/unarchive")(handlers.restore_skill)
    app.post("/api/v0/skills/{skill_id}/versions", status_code=201)(
        handlers.save_personal_version
    )
    app.post("/api/v0/skills/{skill_id}/restore", status_code=201)(
        handlers.restore_release
    )
    app.post("/api/v0/skills/versions/{version_id}/revoke", status_code=201)(
        handlers.revoke_release
    )
    app.post("/api/v0/skills/{skill_id}/publish", status_code=201)(
        handlers.publish_personal_skill
    )


class SkillRouteHandlers:
    """Translate skill service outcomes into transport responses."""

    def __init__(
        self,
        service: SkillsService,
        dump: Callable[[BaseModel], dict[str, Any]],
    ) -> None:
        """Bind the application service and typed contract serializer."""
        self._service = service
        self._dump = dump

    def list_skills(self) -> list[dict[str, Any]]:
        """Return caller-visible catalog entries and their pinned versions."""
        try:
            visible = self._service.list_visible()
            authors = self._service.author_display_names(
                tuple(version for _, version in visible)
            )
            return [
                {
                    "skill": self._dump(definition),
                    "version": self._dump(version),
                    "author_display_name": authors[version.author_principal_id],
                }
                for definition, version in visible
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def validate_package(self, request: SkillPackageRequest) -> dict[str, Any]:
        """Return validated metadata for editor previews without saving files."""
        try:
            return self._service.validate_package(
                request.files, request.required_capability_ids
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def list_versions(self, skill_id: str) -> list[dict[str, Any]]:
        """Return caller-visible immutable release history."""
        try:
            return [self._dump(item) for item in self._service.list_versions(skill_id)]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc

    def get_version(self, version_id: str) -> dict[str, Any]:
        """Return one visible immutable version, including package text files."""
        try:
            return self._dump(self._service.get_version(version_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Skill version not found"
            ) from exc

    def archive_skill(self, skill_id: str) -> dict[str, Any]:
        """Hide a skill from future agent selections."""
        return self._set_archived(skill_id, archived=True)

    def restore_skill(self, skill_id: str) -> dict[str, Any]:
        """Make an archived skill available for new selections again."""
        return self._set_archived(skill_id, archived=False)

    def _set_archived(self, skill_id: str, *, archived: bool) -> dict[str, Any]:
        try:
            return self._dump(self._service.set_archived(skill_id, archived=archived))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc
        except SkillRevisionConflict as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc

    def revoke_release(
        self, version_id: str, request: SkillRevocationRequest
    ) -> dict[str, Any]:
        """Revoke one published version, stopping old agent pins at runtime."""
        try:
            return self._dump(self._service.revoke_release(version_id, request.reason))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Release not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def restore_release(
        self, skill_id: str, request: SkillReleaseRestoreRequest
    ) -> dict[str, Any]:
        """Create a new reviewed release from an older published snapshot."""
        try:
            definition, version = self._service.restore_release(
                skill_id,
                request.source_version_id,
                expected_current_version_id=request.expected_current_version_id,
                self_review_confirmed=request.self_review_confirmed,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc
        except SkillRevisionConflict as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"skill": self._dump(definition), "version": self._dump(version)}

    def create_personal_skill(self, request: SkillPackageRequest) -> dict[str, Any]:
        """Create a private skill package for the authenticated member."""
        try:
            definition = self._service.create_personal_skill(
                request.files, request.required_capability_ids
            )
            version = self._service.get_version(definition.current_version_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"skill": self._dump(definition), "version": self._dump(version)}

    def save_personal_version(
        self,
        skill_id: str,
        request: SkillPackageRequest,
        if_match: str = Header(alias="If-Match"),
    ) -> dict[str, Any]:
        """Validate and save a new immutable version of a private skill."""
        try:
            expected_version = if_match.strip().removeprefix('"').removesuffix('"')
            definition = self._service.save_personal_version(
                skill_id,
                request.files,
                expected_current_version_id=expected_version,
                required_capability_ids=request.required_capability_ids,
            )
            version = self._service.get_version(definition.current_version_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc
        except SkillRevisionConflict as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"skill": self._dump(definition), "version": self._dump(version)}

    def publish_personal_skill(
        self, skill_id: str, request: SkillPublishRequest
    ) -> dict[str, Any]:
        """Publish an exact reviewed personal version into the org catalog."""
        try:
            definition = self._service.publish_personal_skill(
                skill_id,
                request.skill_version_id,
                request.content_digest,
                self_review_confirmed=request.self_review_confirmed,
                expected_org_current_version_id=(
                    request.expected_org_current_version_id
                ),
            )
            version = self._service.get_version(definition.current_version_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc
        except SkillRevisionConflict as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"skill": self._dump(definition), "version": self._dump(version)}
