"""Validate and canonicalize portable Agent Skills packages."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

import yaml

MAX_FILES = 50
MAX_PACKAGE_BYTES = 1_048_576
MAX_RESOURCE_BYTES = 65_536
MAX_SKILL_BYTES = 32_768
_SUPPORTED_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}


def validate_skill_package(
    files: dict[str, str], required_capability_ids: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return parsed metadata, instructions, files and digest for a text bundle."""
    if not files or len(files) > MAX_FILES:
        raise ValueError(f"skill package must contain 1 to {MAX_FILES} files")
    normalized = _normalize_files(files)
    skill_markdown = normalized.get("SKILL.md")
    if skill_markdown is None:
        raise ValueError("skill package must include SKILL.md at its root")
    metadata, instructions = _parse_skill_markdown(skill_markdown)
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not _valid_name(name):
        raise ValueError("SKILL.md name must be a valid lowercase skill slug")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise ValueError("SKILL.md description must contain 1 to 1024 characters")
    if not instructions.strip():
        raise ValueError("SKILL.md must include instruction content after metadata")
    if "allowed-tools" in metadata:
        raise ValueError("allowed-tools is unsupported and never grants Weaves access")
    if len(required_capability_ids) != len(set(required_capability_ids)):
        raise ValueError("required capability IDs must be unique")
    if any(
        not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", item)
        for item in required_capability_ids
    ):
        raise ValueError("required capability IDs must be canonical identifiers")
    return {
        "name": name,
        "description": description.strip(),
        "instructions": instructions.strip(),
        "files": dict(sorted(normalized.items())),
        "required_capability_ids": tuple(sorted(required_capability_ids)),
        "content_digest": _digest(normalized, required_capability_ids),
    }


def _normalize_files(files: dict[str, str]) -> dict[str, str]:
    """Validate paths and enforce per-file and total text package limits."""
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, content in files.items():
        path = _validate_path(raw_path)
        if path in normalized:
            raise ValueError("skill package contains duplicate normalized paths")
        if not isinstance(content, str):
            raise ValueError("skill package files must be UTF-8 text")
        encoded = content.encode("utf-8")
        limit = MAX_SKILL_BYTES if path == "SKILL.md" else MAX_RESOURCE_BYTES
        if len(encoded) > limit:
            raise ValueError(f"{path} exceeds its {limit}-byte limit")
        total_bytes += len(encoded)
        normalized[path] = content
    if total_bytes > MAX_PACKAGE_BYTES:
        raise ValueError("skill package exceeds the 1 MiB total size limit")
    return normalized


def _validate_path(raw_path: str) -> str:
    """Reject unsafe paths and unsupported executable or binary content."""
    if not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise ValueError("skill package contains an invalid file path")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("skill package paths must be relative and traversal-free")
    normalized = path.as_posix()
    if normalized != raw_path or path.parts[0].lower() == "scripts":
        raise ValueError(
            "skill package paths must be normalized; scripts are unsupported"
        )
    if normalized != "SKILL.md" and path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ValueError("skill package contains an unsupported file type")
    return normalized


def _parse_skill_markdown(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter safely and return the procedure body."""
    if not content.startswith("---\n"):
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    frontmatter, separator, body = content[4:].partition("\n---\n")
    if not separator:
        raise ValueError("SKILL.md YAML frontmatter is not closed")
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise ValueError("SKILL.md contains invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict) or not {"name", "description"}.issubset(metadata):
        raise ValueError("SKILL.md frontmatter requires name and description")
    return metadata, body


def _valid_name(value: str) -> bool:
    """Apply portable skill slug constraints from the package specification."""
    return (
        len(value) <= 64
        and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None
    )


def _digest(files: dict[str, str], required_capability_ids: tuple[str, ...]) -> str:
    """Hash the sorted path/content manifest using canonical JSON encoding."""
    manifest = json.dumps(
        {
            "files": files,
            "required_capability_ids": sorted(required_capability_ids),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()
