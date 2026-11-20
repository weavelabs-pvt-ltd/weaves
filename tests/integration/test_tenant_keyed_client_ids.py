"""Client-chosen identifiers never collide across, or reveal, other tenants."""

import pytest

from contracts.v1 import EvaluationSuiteRecord
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.memory_repository import InMemoryRepository
from infrastructure.persistence.postgres_repository import PostgresRepository
from shared.clock import utc_now


def _suite(org_id, workspace_id, name="shared-suite-name"):
    now = utc_now()
    return EvaluationSuiteRecord.model_validate(
        {
            "suite_id": name,
            "org_id": org_id,
            "workspace_id": workspace_id,
            "definition": {
                "suite_id": name,
                "agent_id": f"agent-{org_id}",
                "cases": ({"case_id": "c1", "task": f"task of {org_id}"},),
            },
            "created_by_principal_id": "p",
            "updated_by_principal_id": "p",
            "created_at": now,
            "updated_at": now,
        }
    )


def _check_two_tenants_share_one_name(repository):
    repository.create(_suite("org-a", "ws-a"))
    repository.create(_suite("org-b", "ws-b"))  # no conflict, no existence leak

    a = repository.get_scoped("shared-suite-name", "org-a", "ws-a")
    b = repository.get_scoped("shared-suite-name", "org-b", "ws-b")
    assert a.definition.cases[0].task == "task of org-a"
    assert b.definition.cases[0].task == "task of org-b"
    assert len(repository.list_scoped("org-a")) == 1

    with pytest.raises(ValueError):  # a real duplicate inside one tenant still fails
        repository.create(_suite("org-a", "ws-a"))
    with pytest.raises(KeyError):
        repository.get_scoped("shared-suite-name", "org-c")
    updated = a.model_copy(update={"updated_by_principal_id": "other"})
    repository.put(updated)
    assert (
        repository.get_scoped("shared-suite-name", "org-a").updated_by_principal_id
        == "other"
    )
    assert (
        repository.get_scoped("shared-suite-name", "org-b").updated_by_principal_id
        == "p"
    )


def test_in_memory_tenants_can_reuse_the_same_client_chosen_name():
    _check_two_tenants_share_one_name(InMemoryRepository("suite_id", tenant_keyed=True))


def test_postgres_tenants_can_reuse_the_same_client_chosen_name(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        repository = PostgresRepository(
            store,
            "evaluation_suites",
            "suite_id",
            EvaluationSuiteRecord,
            tenant_keyed=True,
        )
        _check_two_tenants_share_one_name(repository)
    finally:
        store.close()


def test_legacy_unprefixed_rows_remain_readable_by_their_own_organization(
    isolated_postgres_url,
):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        legacy = PostgresRepository(
            store, "evaluation_suites", "suite_id", EvaluationSuiteRecord
        )
        legacy.create(_suite("org-a", "ws-a", "old-suite"))
        keyed = PostgresRepository(
            store,
            "evaluation_suites",
            "suite_id",
            EvaluationSuiteRecord,
            tenant_keyed=True,
        )
        assert keyed.get_scoped("old-suite", "org-a").suite_id == "old-suite"
        with pytest.raises(KeyError):
            keyed.get_scoped("old-suite", "org-b")
    finally:
        store.close()
