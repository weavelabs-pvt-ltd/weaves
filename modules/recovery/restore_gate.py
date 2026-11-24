"""Restore markers: services refuse to start between a restore and its reconcile.

``mark_restore`` is run before a database restore and writes
``restore-pending-<id>.json`` into the tombstone directory (on disk, outside the
database being replaced). ``restore_reconcile.py apply`` writes
``restore-reconciled-<id>.json`` only after every replay step succeeded. The API
and worker call ``assert_from_env`` and the release script checks the same
files, so a restored database is never served before reconciliation.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RestoreNotReconciled(RuntimeError):
    """A restore marker exists without a completed reconciliation."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n")
    with open(temp, "rb") as handle:
        os.fsync(handle.fileno())
    temp.replace(path)


def mark_restore(directory: Path, backup_name: str) -> str:
    """Record that a restore of ``backup_name`` is about to replace the database."""
    restore_id = uuid.uuid4().hex[:16]
    _write(
        directory / f"restore-pending-{restore_id}.json",
        {"id": restore_id, "backup": backup_name, "marked_at": _now()},
    )
    return restore_id


def mark_reconciled(directory: Path, restore_id: str, report: dict[str, Any]) -> None:
    """Record that reconciliation completed; clears the start gate."""
    _write(directory / f"reconcile-report-{restore_id}.json", report)
    _write(
        directory / f"restore-reconciled-{restore_id}.json",
        {"id": restore_id, "reconciled_at": _now()},
    )


def pending_restores(directory: Path) -> list[str]:
    """Ids of restores marked but not yet reconciled."""
    if not directory.is_dir():
        return []
    marked = {
        p.name[len("restore-pending-") : -len(".json")]
        for p in directory.glob("restore-pending-*.json")
    }
    done = {
        p.name[len("restore-reconciled-") : -len(".json")]
        for p in directory.glob("restore-reconciled-*.json")
    }
    return sorted(marked - done)


def assert_services_may_start(directory: Path | str | None) -> None:
    """Raise while a restore is pending or the tombstone directory is unusable."""
    if directory is None or not str(directory).strip():
        return
    path = Path(directory)
    pending = pending_restores(path)
    if pending:
        raise RestoreNotReconciled(
            "database restore not reconciled; run restore_reconcile.py apply "
            f"(pending: {', '.join(pending)})"
        )
    if path.exists() and not os.access(path, os.W_OK | os.X_OK):
        raise RestoreNotReconciled(f"tombstone directory is not writable: {path}")


def assert_from_env(env: dict[str, str] | None = None) -> None:
    """``assert_services_may_start`` for ``WEAVES_TOMBSTONE_DIR``."""
    source = env if env is not None else os.environ
    assert_services_may_start(source.get("WEAVES_TOMBSTONE_DIR"))
