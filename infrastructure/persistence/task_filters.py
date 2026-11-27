"""Task list filters shared by the repository adapters."""

from __future__ import annotations


def origin_matches(record: object, kind: str | None) -> bool:
    """Whether a task came from ``kind`` (``None`` keeps every task)."""
    origin = getattr(record, "origin", None)
    return kind is None or getattr(origin, "kind", None) == kind
