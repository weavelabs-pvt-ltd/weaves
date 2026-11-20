"""The in-memory secret store enforces the same tenant ownership."""

import pytest

from infrastructure.secrets.store import InMemorySecretStore
from shared.request_context import reset_request_principal, set_request_identity


def test_in_memory_secrets_are_tenant_owned():
    secrets = InMemorySecretStore()
    token = set_request_identity("p", "org-a", "ws")
    ref = secrets.put("value", purpose="connector")
    assert secrets.get(ref) == "value"
    reset_request_principal(token)

    token = set_request_identity("p", "org-b", "ws")
    with pytest.raises(KeyError):
        secrets.get(ref)
    with pytest.raises(KeyError):
        secrets.replace(ref, "other")
    secrets.delete(ref)
    reset_request_principal(token)

    token = set_request_identity("p", "org-a", "ws")
    assert secrets.get(ref) == "value"
    reset_request_principal(token)
