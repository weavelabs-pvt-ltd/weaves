"""Atomic snapshots for groups of in-memory repository writes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from infrastructure.persistence.memory_repository import InMemoryRepository


class InMemoryUnitOfWork:
    """Lock and snapshot a set of in-memory repositories as one local transaction."""

    def __init__(self, repositories: Iterable[InMemoryRepository[Any]]) -> None:
        """Initialize the adapter with its identifier or resource configuration."""
        self._repositories = tuple(
            sorted(
                {id(repository): repository for repository in repositories}.values(),
                key=id,
            )
        )
        self._snapshots: dict[int, Any] = {}

    def __enter__(self) -> InMemoryUnitOfWork:
        """Acquire repository locks and create rollback snapshots."""
        acquired: list[InMemoryRepository[Any]] = []
        try:
            for repository in self._repositories:
                repository._lock.acquire()
                acquired.append(repository)
            self._snapshots = {
                id(repository): {
                    key: repository._clone(record)
                    for key, record in repository._records.items()
                }
                for repository in self._repositories
            }
        except Exception:
            for repository in reversed(acquired):
                repository._lock.release()
            raise
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Literal[False]:
        """Restore snapshots on failure and release all repository locks."""
        try:
            if exc_type is not None:
                for repository in self._repositories:
                    repository._records = self._snapshots[id(repository)]
        finally:
            for repository in reversed(self._repositories):
                repository._lock.release()
        return False
