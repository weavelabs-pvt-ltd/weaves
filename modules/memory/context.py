"""Deterministic retrieval for explicit memory records."""

import re
from collections.abc import Iterable
from datetime import UTC, datetime

from contracts.v1 import MemoryItem, MemoryPolicy, MemoryScope


class MemoryContextBuilder:
    """Select visible, non-expired memory and render source-addressable records."""

    def build(
        self,
        *,
        items: Iterable[MemoryItem],
        policy: MemoryPolicy,
        org_id: str,
        workspace_id: str,
        query: str,
        workflow_id: str | None = None,
        thread_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, str], ...]:
        if not policy.max_items or policy.allowed_scopes == (MemoryScope.NONE,):
            return ()
        timestamp = now or datetime.now(UTC)
        query_terms = _terms(query)
        candidates: list[tuple[int, datetime, MemoryItem]] = []
        for item in items:
            if item.org_id != org_id or item.scope not in policy.allowed_scopes:
                continue
            if (
                item.scope is MemoryScope.WORKSPACE
                and item.workspace_id != workspace_id
            ):
                continue
            if item.scope is MemoryScope.WORKFLOW and item.scope_ref != workflow_id:
                continue
            if item.scope is MemoryScope.THREAD and item.scope_ref != thread_id:
                continue
            if item.expires_at and item.expires_at <= timestamp:
                continue
            score = len(query_terms & _terms(f"{item.title} {item.text}"))
            if not score:
                continue
            candidates.append((score, item.created_at, item))
        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return tuple(
            {
                "title": item.title,
                "text": item.text,
                "source_id": item.source_ref or f"memory:{item.memory_id}",
                "memory_id": item.memory_id,
                "scope": item.scope.value,
            }
            for _, _, item in candidates[: policy.max_items]
        )


def _terms(value: str) -> set[str]:
    return {part for part in re.findall(r"[\w'-]+", value.casefold()) if len(part) > 2}


__all__ = ["MemoryContextBuilder"]
