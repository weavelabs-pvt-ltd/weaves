"""Production processes refuse implicit local tenant scope."""

import pytest

from shared.errors import TenantContextRequired
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    set_request_principal,
    tenant_context_required,
)


@pytest.fixture
def strict():
    require_tenant_context(True)
    yield
    require_tenant_context(False)


def test_local_defaults_remain_available_without_strict_mode():
    assert not tenant_context_required()
    assert effective_org_id() == "local-org"
    assert effective_workspace_id() == "local-workspace"
    assert effective_principal_id("local-developer") == "local-developer"


def test_strict_mode_rejects_missing_context_everywhere(strict):
    for call in (
        effective_org_id,
        effective_workspace_id,
        lambda: effective_principal_id("local-developer"),
        lambda: set_request_principal("local-developer"),
    ):
        with pytest.raises(TenantContextRequired):
            call()


def test_strict_mode_uses_validated_identity_and_does_not_leak_after_reset(strict):
    token = set_request_identity("principal-a", "org-a", "ws-a")
    assert effective_org_id() == "org-a"
    assert effective_workspace_id() == "ws-a"
    assert effective_principal_id("ignored") == "principal-a"
    reset_request_principal(token)
    with pytest.raises(TenantContextRequired):
        effective_org_id()


@pytest.mark.parametrize(
    ("database", "seed_local", "expected"),
    [
        (True, "false", True),
        (True, "true", False),
        (False, "false", False),
    ],
)
def test_process_wiring_enables_strict_context_only_for_durable_non_demo_runtimes(
    monkeypatch, database, seed_local, expected
):
    from apps import runtime_wiring

    class Pools:
        database_url = "postgresql://unused" if database else None

    monkeypatch.setenv("WEAVES_SEED_LOCAL_DEFAULTS", seed_local)
    monkeypatch.setattr(runtime_wiring, "load_pool_settings", lambda process: Pools())
    monkeypatch.setattr(runtime_wiring, "open_http_client", lambda settings: object())
    monkeypatch.setattr(
        runtime_wiring,
        "_open_store",
        lambda settings, app_role=None: object() if database else None,
    )
    monkeypatch.setattr(
        runtime_wiring, "compose_platform_runtime", lambda **kwargs: object()
    )
    require_tenant_context(False)
    try:
        runtime_wiring.open_process_runtime(process="api")
        assert tenant_context_required() is expected
    finally:
        require_tenant_context(False)
