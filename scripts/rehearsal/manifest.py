"""The release-compat manifest: what was rehearsed and the release gate."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

VERSION = 1
SHA = re.compile(r"[0-9a-f]{40}")


def load_policy(path: Path) -> dict[str, Any]:
    """Read the rollback policy and require a pinned minimum rollback revision."""
    policy = json.loads(path.read_text())
    revision = policy.get("min_rollback_revision")
    minimum = policy.get("min_rollback_schema_version")
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError("release_policy.json needs a min_rollback_revision commit SHA")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise ValueError("min_rollback_schema_version must be a positive integer")
    return policy  # type: ignore[no-any-return]


def gate_problems(
    manifest: dict[str, Any], policy: dict[str, Any], revision: str
) -> list[str]:
    """Why a release must not deploy on this manifest (empty means it may)."""
    problems: list[str] = []
    if manifest.get("version") != VERSION:
        problems.append("unknown manifest version")
    if manifest.get("result") != "passed":
        problems.append("the rehearsal did not pass")
    if manifest.get("candidate", {}).get("revision") != revision:
        problems.append("the manifest is for a different candidate revision")
    if manifest.get("candidate", {}).get("dirty"):
        problems.append("the candidate had uncommitted changes")
    floor = manifest.get("rollback_floor", {})
    if floor.get("revision") != policy["min_rollback_revision"]:
        problems.append("the rehearsal did not use the minimum rollback revision")
    head = floor.get("schema_head")
    if not isinstance(head, int) or head < policy["min_rollback_schema_version"]:
        problems.append("the rollback revision is below the minimum schema version")
    if manifest.get("database_head_after_upgrade") != manifest.get("candidate", {}).get(
        "schema_head"
    ):
        problems.append("the upgraded database is not at the candidate schema head")
    if not manifest.get("steps") or not all(s.get("ok") for s in manifest["steps"]):
        problems.append("a rehearsal step failed or none were recorded")
    return problems


def verify_manifest(path: Path, policy_path: Path, revision: str) -> list[str]:
    """Load a manifest file fail-closed: any problem, including absence, blocks."""
    try:
        manifest = json.loads(path.read_text())
        policy = load_policy(policy_path)
    except (OSError, ValueError) as error:
        return [f"manifest or policy unreadable: {type(error).__name__}"]
    return gate_problems(manifest, policy, revision)
