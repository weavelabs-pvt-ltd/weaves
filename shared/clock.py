"""UTC timestamps shared across product modules."""

from datetime import UTC, datetime

from shared.ids import new_id

__all__ = ["new_id", "utc_now"]


def utc_now() -> datetime:
    return datetime.now(UTC)
