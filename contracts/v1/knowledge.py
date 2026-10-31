"""Contracts for organization supplied context documents in the local v0."""

from pydantic import Field

from contracts.v1.base import (
    DisplayName,
    MutableWorkspaceScopedContract,
    OpaqueId,
)


class KnowledgeDocument(MutableWorkspaceScopedContract):
    document_id: OpaqueId
    source_id: OpaqueId
    title: DisplayName
    text: str = Field(min_length=1, max_length=12_000)
    is_seeded: bool = False


__all__ = ["KnowledgeDocument"]
