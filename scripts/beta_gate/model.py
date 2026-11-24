"""Result types shared by every private-beta gate check."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PASS = "pass"
FAIL = "fail"
BLOCKED = "blocked-on-owner"
UNVERIFIED = "unverified"
STATUSES = (PASS, FAIL, BLOCKED, UNVERIFIED)


@dataclass(frozen=True)
class Result:
    """One gate item: what was checked, the verdict and what would change it."""

    id: str
    title: str
    status: str
    detail: str
    next_step: str = ""

    def as_json(self) -> dict[str, str]:
        """Plain dict for ``--json``."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class Context:
    """Everything a check may read; checks never write or run commands."""

    root: Path
    evidence_dir: Path
    production: dict[str, Any] | None
    github_protected: bool | None
    now: datetime

    def file(self, relative: str) -> Path:
        """A path inside the repository checkout."""
        return self.root / relative


def make(id: str, title: str, ok: bool, detail: str, next_step: str = "") -> Result:
    """PASS when ``ok`` else FAIL."""
    return Result(id, title, PASS if ok else FAIL, detail, "" if ok else next_step)


def blocked(id: str, title: str, detail: str, next_step: str) -> Result:
    """The item needs an owner decision that has not been made."""
    return Result(id, title, BLOCKED, detail, next_step)


def unverified(id: str, title: str, detail: str, next_step: str) -> Result:
    """Production evidence was not supplied, so nothing can be claimed."""
    return Result(id, title, UNVERIFIED, detail, next_step)


def load_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None when absent or unreadable."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def age_days(stamp: Any, now: datetime) -> float | None:
    """Days since an ISO-8601 timestamp, or None if it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds() / 86400
