"""Field-equality and timestamp-bound lookups for the in-memory repository."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any


def _plain(value: Any) -> Any:
    """Enum members compare by their stored value."""
    return getattr(value, "value", value)


def _text(value: Any) -> str:
    """A field as the stored JSON text (``true``/``false`` for booleans)."""
    value = _plain(value)
    return str(value).lower() if isinstance(value, bool) else str(value)


def _status_text(record: Any) -> Any:
    """A record's status as the stored string."""
    return _plain(getattr(record, "status", None))


class InMemoryFieldQueries[TContract]:
    """Mirror the indexed PostgreSQL lookups over the in-memory records."""

    _lock: RLock
    _records: dict[str, TContract]

    def _record_id(self, record: TContract) -> str:
        """Return the record identifier."""
        raise NotImplementedError

    def _clone(self, record: TContract) -> TContract:
        """Return a detached copy of one record."""
        raise NotImplementedError

    def list_by_field(
        self, field: str, value: str, *, casefold: bool = False
    ) -> tuple[TContract, ...]:
        """Return every record whose field equals the value, by identifier."""
        wanted = value.casefold() if casefold else value
        with self._lock:
            matches = []
            for record in self._records.values():
                current: Any = getattr(record, field, None)
                current = getattr(current, "value", current)
                if casefold and isinstance(current, str):
                    current = current.casefold()
                if current == wanted:
                    matches.append(record)
            matches.sort(key=self._record_id)
            return tuple(self._clone(record) for record in matches)

    def list_before(
        self,
        field: str,
        before: datetime,
        *,
        limit: int,
        equals: dict[str, str] | None = None,
    ) -> tuple[TContract, ...]:
        """Return a bounded page of records whose timestamp is before a cutoff."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            stamped = [
                (stamp, self._record_id(record), record)
                for record in self._records.values()
                if isinstance(stamp := getattr(record, field, None), datetime)
                and stamp < before
                and all(
                    _text(getattr(record, name, None)) == value
                    for name, value in (equals or {}).items()
                )
            ]
            stamped.sort(key=lambda item: (item[0], item[1]))
            return tuple(self._clone(item[2]) for item in stamped[:limit])

    def list_by_status(
        self, status: str, *, limit: int, after_id: str | None = None
    ) -> tuple[TContract, ...]:
        """Return a bounded, identifier-ordered page of records in one status."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            selected = sorted(
                (
                    record
                    for record in self._records.values()
                    if _status_text(record) == status
                    and (after_id is None or self._record_id(record) > after_id)
                ),
                key=self._record_id,
            )
            return tuple(self._clone(record) for record in selected[:limit])

    def list_recent_page(
        self,
        org_id: str,
        workspace_id: str | None,
        *,
        filters: dict[str, str],
        cursor_created_at: datetime | None,
        cursor_id: str | None,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return one newest-first keyset page of a tenant's records."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            selected = [
                record
                for record in self._records.values()
                if getattr(record, "org_id", None) == org_id
                and (
                    workspace_id is None
                    or getattr(record, "workspace_id", None) == workspace_id
                )
                and all(
                    _plain(getattr(record, field, None)) == value
                    for field, value in filters.items()
                )
            ]
            selected.sort(
                key=lambda record: (
                    getattr(record, "created_at"),  # noqa: B009
                    self._record_id(record),
                ),
                reverse=True,
            )
            if cursor_created_at is not None:
                if getattr(self, "_tenant_keyed", False):
                    cursor_id = f"{org_id}:{cursor_id}"
                position = (cursor_created_at, cursor_id or "")
                selected = [
                    record
                    for record in selected
                    if (getattr(record, "created_at"), self._record_id(record))  # noqa: B009
                    < position
                ]
            return tuple(self._clone(record) for record in selected[:limit])

    def get_many_scoped(
        self,
        record_ids: tuple[str, ...],
        org_id: str,
        workspace_id: str | None = None,
    ) -> tuple[TContract, ...]:
        """Return the tenant's records among ``record_ids`` (missing ones skipped)."""
        wanted = set(record_ids)
        with self._lock:
            matches = sorted(
                (
                    record
                    for record in self._records.values()
                    if self._record_id(record) in wanted
                    and getattr(record, "org_id", None) == org_id
                    and (
                        workspace_id is None
                        or getattr(record, "workspace_id", None) == workspace_id
                    )
                ),
                key=self._record_id,
            )
            return tuple(self._clone(record) for record in matches)

    def list_by_field_in(
        self,
        field: str,
        values: tuple[str, ...],
        *,
        org_id: str,
        workspace_id: str | None = None,
    ) -> tuple[TContract, ...]:
        """Return a tenant's records whose field equals any of ``values``."""
        wanted = set(values)
        with self._lock:
            matches = sorted(
                (
                    record
                    for record in self._records.values()
                    if getattr(record, "org_id", None) == org_id
                    and (
                        workspace_id is None
                        or getattr(record, "workspace_id", None) == workspace_id
                    )
                    and _plain(getattr(record, field, None)) in wanted
                ),
                key=self._record_id,
            )
            return tuple(self._clone(record) for record in matches)

    def find_scoped_by_field(
        self,
        field: str,
        value: str,
        *,
        org_id: str,
        casefold: bool = False,
        exclude_status: str | None = None,
    ) -> TContract | None:
        """Return one record of an organization whose field equals ``value``."""
        wanted = value.casefold() if casefold else value
        with self._lock:
            for record in sorted(self._records.values(), key=self._record_id):
                current = _plain(getattr(record, field, None))
                if casefold and isinstance(current, str):
                    current = current.casefold()
                if (
                    getattr(record, "org_id", None) == org_id
                    and current == wanted
                    and (
                        exclude_status is None or _status_text(record) != exclude_status
                    )
                ):
                    return self._clone(record)
        return None
