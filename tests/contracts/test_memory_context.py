from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contracts.v1 import (
    MemoryItem,
    MemoryPolicy,
    MemoryRetentionClass,
    MemoryScope,
)
from modules.memory.context import MemoryContextBuilder


def _item(**overrides):
    values = {
        "memory_id": "memory-a",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "scope": MemoryScope.WORKSPACE,
        "title": "Release process",
        "text": "Release checklist is reviewed by the release owner.",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    return MemoryItem(**(values | overrides))


def test_memory_policy_disables_context_without_ambiguous_scopes():
    with pytest.raises(ValidationError):
        MemoryPolicy(allowed_scopes=(MemoryScope.NONE, MemoryScope.WORKSPACE))
    policy = MemoryPolicy(allowed_scopes=(MemoryScope.NONE,), max_items=0)
    assert policy.max_items == 0


def test_memory_retrieval_applies_scope_expiry_and_item_limit():
    now = datetime.now(UTC)
    records = (
        _item(
            memory_id="expired",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
            expires_at=now - timedelta(seconds=1),
        ),
        _item(memory_id="other-workspace", workspace_id="workspace-b"),
        _item(
            memory_id="release-a",
            title="Release process",
            text="Release owner checklist.",
        ),
        _item(
            memory_id="release-b",
            title="Release calendar",
            text="Calendar dates for release.",
        ),
    )
    result = MemoryContextBuilder().build(
        items=records,
        policy=MemoryPolicy(max_items=1),
        org_id="org-a",
        workspace_id="workspace-a",
        query="release owner",
        now=now,
    )
    assert len(result) == 1
    assert result[0]["memory_id"] == "release-a"


def test_organization_memory_requires_no_workspace_scope():
    item = _item(scope=MemoryScope.ORGANIZATION, workspace_id=None)
    assert item.workspace_id is None


def test_ephemeral_memory_requires_expiry():
    with pytest.raises(ValidationError, match="ephemeral memory requires expires_at"):
        _item(retention_class=MemoryRetentionClass.EPHEMERAL)


def test_thread_and_workflow_memory_are_retrieved_only_in_matching_scope():
    items = (
        _item(
            memory_id="thread-a-memory",
            scope=MemoryScope.THREAD,
            scope_ref="thread-a",
        ),
        _item(
            memory_id="thread-b-memory",
            scope=MemoryScope.THREAD,
            scope_ref="thread-b",
        ),
        _item(
            memory_id="workflow-a-memory",
            scope=MemoryScope.WORKFLOW,
            scope_ref="workflow-a",
        ),
    )
    policy = MemoryPolicy(
        allowed_scopes=(MemoryScope.THREAD, MemoryScope.WORKFLOW),
    )
    builder = MemoryContextBuilder()
    scoped = builder.build(
        items=items,
        policy=policy,
        org_id="org-a",
        workspace_id="workspace-a",
        query="release checklist",
        workflow_id="workflow-a",
        thread_id="thread-a",
    )
    assert {item["memory_id"] for item in scoped} == {
        "thread-a-memory",
        "workflow-a-memory",
    }
    workflow_only = builder.build(
        items=items,
        policy=policy,
        org_id="org-a",
        workspace_id="workspace-a",
        query="release checklist",
        workflow_id="workflow-a",
    )
    assert [item["memory_id"] for item in workflow_only] == ["workflow-a-memory"]
