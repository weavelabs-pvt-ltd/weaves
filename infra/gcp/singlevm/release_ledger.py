"""Append-only ledger of every release the VM has applied, and rollback safety rules.

``releases.jsonl`` keeps one JSON object per line and is only ever appended to,
under the VM release lock. ``releases.json`` (the latest image per component)
stays for existing readers. Apart from the optional schema probe this module has
no Docker dependency, so the rules are testable without a VM.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from release_runtime import atomic_write, run

LEDGER = "releases.jsonl"
POLICY = "release_policy.json"
LEGACY_RECORD = "releases.json"
RESULTS = ("healthy", "failed")

Entry = dict[str, Any]


class RollbackRefused(ValueError):
    """The requested release is not a safe rollback target."""


def _imported(root: Path) -> list[Entry]:
    """Entries for a pre-ledger ``releases.json``: the latest image per component."""
    record = root / LEGACY_RECORD
    if not record.exists():
        return []
    stamp = datetime.fromtimestamp(record.stat().st_mtime, UTC)
    latest = json.loads(record.read_text())
    return [
        {
            "seq": number,
            "time": stamp.isoformat(timespec="seconds"),
            "component": component,
            "image": latest[component]["image"],
            "revision": latest[component]["revision"],
            "schema_version": None,
            "result": "healthy",
            "action": "import",
        }
        for number, component in enumerate(sorted(latest), start=1)
    ]


def read_ledger(root: Path) -> list[Entry]:
    """Return the ledger, or a non-persisted view of an old ``releases.json``."""
    path = root / LEDGER
    if not path.exists():
        return _imported(root)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def ensure_ledger(root: Path) -> None:
    """Write the one-time import of an existing ``releases.json`` (idempotent)."""
    if (root / LEDGER).exists() or not (root / LEGACY_RECORD).exists():
        return
    atomic_write(
        root / LEDGER,
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in _imported(root)),
    )


def record_release(
    root: Path,
    component: str,
    image: str,
    revision: str,
    schema_version: int | None,
    result: str,
    action: str = "deploy",
) -> Entry:
    """Append one durable line; callers hold the release lock."""
    if result not in RESULTS:
        raise ValueError("Unknown release result")
    ensure_ledger(root)
    entries = read_ledger(root)
    entry: Entry = {
        "seq": entries[-1]["seq"] + 1 if entries else 1,
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "component": component,
        "image": image,
        "revision": revision,
        "schema_version": schema_version,
        "result": result,
        "action": action,
    }
    line = (json.dumps(entry, sort_keys=True) + "\n").encode()
    descriptor = os.open(root / LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return entry


def image_schema_version(image: str) -> int | None:
    """Ask the pulled backend image for its migration head (null if unknown)."""
    code = "from infrastructure.database.store import SCHEMA_HEAD_VERSION as v;print(v)"
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none"]
    try:
        return int(run([*command, "--entrypoint", "python", image, "-c", code]).strip())
    except (RuntimeError, ValueError):
        return None


def load_policy(path: Path) -> dict[str, Any]:
    """Read and validate the rollback policy; a bad policy blocks rollbacks."""
    try:
        policy = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RollbackRefused("The rollback policy is missing or invalid") from error
    minimum = policy.get("min_rollback_schema_version")
    revision = policy.get("min_rollback_revision")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise RollbackRefused("min_rollback_schema_version must be a positive integer")
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", str(revision)):
        raise RollbackRefused("min_rollback_revision must be null or a commit SHA")
    return policy  # type: ignore[no-any-return]


def _eligible(entry: Entry, policy: dict[str, Any]) -> bool:
    version = entry.get("schema_version")
    return (
        entry["result"] == "healthy"
        and isinstance(version, int)
        and version >= policy["min_rollback_schema_version"]
    )


def rollback_floor(entries: list[Entry], policy: dict[str, Any]) -> int | None:
    """Sequence of the first healthy backend release at the breaking schema."""
    return min(
        (
            e["seq"]
            for e in entries
            if e["component"] == "backend" and _eligible(e, policy)
        ),
        default=None,
    )


def check_rollback(entries: list[Entry], policy: dict[str, Any], target: Entry) -> None:
    """Raise unless ``target`` may be redeployed without breaking the database."""
    if target["result"] != "healthy":
        raise RollbackRefused("That release never became healthy")
    if target["component"] != "backend":
        return
    floor = rollback_floor(entries, policy)
    if floor is None or target["seq"] < floor or not _eligible(target, policy):
        raise RollbackRefused(
            f"Release {target['seq']} is below the minimum rollback revision "
            f"(schema head {policy['min_rollback_schema_version']} or newer; "
            f"first recorded at seq {floor}); roll forward instead"
        )


def find_target(entries: list[Entry], component: str, reference: str) -> Entry:
    """Pick a ledger entry by sequence number or by (a unique prefix of) digest."""
    pool = [e for e in entries if e["component"] == component]
    if reference.isdigit():
        matches = [e for e in pool if e["seq"] == int(reference)]
    else:
        wanted = reference.removeprefix("sha256:")
        matches = [
            e for e in pool if e["image"].split("@sha256:")[-1].startswith(wanted)
        ]
    if not matches or len({e["image"] for e in matches}) != 1:
        raise RollbackRefused(f"No unique {component} release matches {reference!r}")
    return matches[0]


def refuse_unsafe_redeploy(
    root: Path, component: str, image: str, policy_path: Path
) -> None:
    """Block a manual deploy of a digest the ledger knows to be below the floor."""
    entries = read_ledger(root)
    known = [e for e in entries if e["component"] == component and e["image"] == image]
    if component != "backend" or not known or not policy_path.exists():
        return
    policy = load_policy(policy_path)
    floor = rollback_floor(entries, policy)
    if floor is None or not any(
        _eligible(e, policy) and e["seq"] >= floor for e in known
    ):
        raise RollbackRefused(
            "That digest was released below the minimum rollback revision"
        )


def format_entries(entries: list[Entry], floor: int | None) -> str:
    """Render the ledger as a plain table, marking the rollback floor."""
    rows = [
        "seq   time                      component  result   schema  revision  image"
    ]
    for e in entries:
        mark = "*" if e["seq"] == floor else " "
        version = "-" if e["schema_version"] is None else e["schema_version"]
        rows.append(
            f"{e['seq']:<4}{mark} {e['time']:<25} {e['component']:<10} "
            f"{e['result']:<8} {version!s:<7} {e['revision'][:8]:<9} {e['image']}"
        )
    rows.append("* minimum rollback revision (oldest backend release safe to restore)")
    return "\n".join(rows)
