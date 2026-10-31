"""Named product repositories supplied by persistence composition roots."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TypeVar, cast

from application.ports.repository import Repository
from contracts.v1.base import ProductContract

TProduct = TypeVar("TProduct", bound=ProductContract)


class ProductRepositorySet:
    """Expose typed repository lookup without importing adapter implementations."""

    def __init__(self, repositories: Mapping[str, object]) -> None:
        """Bind the named repositories created for one platform process."""
        self._repositories = dict(repositories)

    def get(self, name: str) -> Repository[TProduct]:
        """Return one repository by its stable collection name."""
        try:
            repository = self._repositories[name]
        except KeyError as exc:
            raise KeyError(f"repository is not configured: {name}") from exc
        return cast(Repository[TProduct], repository)

    def all(self) -> Iterator[Repository[ProductContract]]:
        """Iterate over configured repositories for transaction composition."""
        for repository in self._repositories.values():
            yield cast(Repository[ProductContract], repository)
