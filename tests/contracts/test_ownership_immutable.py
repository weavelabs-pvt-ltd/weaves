"""A record's organization and workspace cannot be rewritten by a later write."""

import pytest

from contracts.v1 import RoleBinding
from infrastructure.persistence.memory_repository import InMemoryRepository
from shared.clock import utc_now
from shared.errors import OwnershipConflict


def _binding(org_id="org-a", workspace_id="ws-a", role_id="role-1"):
    return RoleBinding(
        id="binding-1",
        org_id=org_id,
        principal_id="principal-1",
        role_id=role_id,
        workspace_id=workspace_id,
        created_at=utc_now(),
    )


def _repository():
    repository = InMemoryRepository("id")
    repository.create(_binding())
    return repository


def test_same_owner_updates_are_allowed():
    repository = _repository()
    repository.put(_binding(role_id="role-2"))
    assert repository.get("binding-1").role_id == "role-2"


@pytest.mark.parametrize(
    "replacement",
    [
        _binding(org_id="org-b"),
        _binding(workspace_id="ws-b"),
        _binding(workspace_id=None),
    ],
)
def test_ownership_cannot_be_transferred_by_put(replacement):
    repository = _repository()
    with pytest.raises(OwnershipConflict):
        repository.put(replacement)
    stored = repository.get("binding-1")
    assert (stored.org_id, stored.workspace_id) == ("org-a", "ws-a")
