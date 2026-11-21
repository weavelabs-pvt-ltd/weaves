"""Production initialization must leave work lists empty across restarts."""

import httpx
import pytest

from apps.runtime_wiring import compose_platform_runtime, open_process_runtime
from tests.support.organizations import create_organization


def test_production_bootstrap_omits_demo_accounts_and_work():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    try:
        runtime.bootstrap()
        assert not runtime.organizations.list()
        assert not runtime.users.list()
        assert not runtime.providers.list()
        assert not runtime.approval_policies.list()
        assert runtime.plugins.list()
        owner = create_organization(
            runtime,
            name="Fresh workspace",
            email="owner@example.com",
            display_name="Owner",
        )
        runtime._bootstrapped = False
        runtime.bootstrap()
        assert [org.id for org in runtime.organizations.list()] == [owner[0].id]
        assert len(runtime.users.list()) == 1
        assert len(runtime.workspaces.list()) == 1
        assert len(runtime.roles.list()) == 3
        assert not runtime.agents.list()
        assert not runtime.agent_versions.list()
        assert not runtime.workflows.list()
        assert not runtime.workflow_versions.list()
        assert not runtime.tasks.list()
        assert not runtime.skill_definitions.list()
        assert all(item.org_id == owner[0].id for item in runtime.providers.list())
    finally:
        runtime.close()


def test_process_wiring_honors_disabled_local_seeding(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WEAVES_SEED_LOCAL_DEFAULTS", "false")
    runtime = open_process_runtime(process="worker")
    try:
        runtime.bootstrap()
        assert runtime.seed_local_defaults is False
        assert not runtime.users.list()
        assert not runtime.agents.list()
    finally:
        runtime.close()


def test_invalid_seed_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("WEAVES_SEED_LOCAL_DEFAULTS", "flase")
    with pytest.raises(ValueError, match="WEAVES_SEED_LOCAL_DEFAULTS"):
        open_process_runtime(process="api")
