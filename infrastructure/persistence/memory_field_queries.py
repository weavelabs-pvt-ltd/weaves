"""Field-equality and timestamp-bound lookups for the in-memory repository."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any


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
        self, field: str, before: datetime, *, limit: int
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
            ]
            stamped.sort(key=lambda item: (item[0], item[1]))
            return tuple(self._clone(item[2]) for item in stamped[:limit])
