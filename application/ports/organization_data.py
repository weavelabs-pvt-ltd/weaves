"""Bulk, organization-wide record access for export and resumable deletion.

Both operations are bounded: pages and delete batches have an explicit limit,
and every call is confined to one organization by ``org_id`` (and, in
PostgreSQL, by that organization's own row-level-security scope).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any, Protocol

# Audit actions kept after deletion: (payload field, action prefixes).
RetainRule = tuple[str, tuple[str, ...]]


class OrganizationData(Protocol):
    """Raw JSON access to one organization's rows across all collections."""

    def page(
        self, collection: str, org_id: str, *, after: str | None, limit: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return up to ``limit`` (record id, payload) pairs after a keyset cursor."""
        ...

    def delete_batch(
        self,
        collection: str,
        org_id: str,
        *,
        limit: int,
        retain: RetainRule | None = None,
    ) -> int:
        """Delete up to ``limit`` rows of one collection; return the count."""
        ...

    def delete_remaining_batch(
        self, org_id: str, *, keep_collections: Collection[str], limit: int
    ) -> int:
        """Delete up to ``limit`` rows of any collection not in the kept set."""
        ...

    def cancel_work(self, org_id: str, *, now: datetime, limit: int) -> int:
        """Cancel queued/running execution jobs and queued task turns."""
        ...

    def count_remaining(self, org_id: str, *, keep_collections: Collection[str]) -> int:
        """Count rows outside the kept collections (verification only)."""
        ...
