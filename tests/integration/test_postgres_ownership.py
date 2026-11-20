"""PostgreSQL writes cannot move a record to another organization or workspace."""

from datetime import UTC, datetime

import pytest

from contracts.v1 import ExecutionJob, ExecutionJobStatus
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from shared.errors import OwnershipConflict


def _job(**changes):
    now = datetime.now(UTC)
    values = {
        "job_id": "job-a",
        "org_id": "org-a",
        "workspace_id": "ws-a",
        "requested_by_principal_id": "principal-a",
        "task": "Run",
        "status": ExecutionJobStatus.RUNNING,
        "attempts": 1,
        "started_at": now,
        "worker_id": "worker-a",
        "created_at": now,
        "updated_at": now,
    }
    return ExecutionJob(**{**values, **changes})


def test_put_and_conditional_updates_cannot_transfer_ownership(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        jobs = create_product_repository_set(store).get("execution_jobs")
        original = jobs.create(_job())

        with pytest.raises(OwnershipConflict):
            jobs.put(_job(org_id="org-b"))
        with pytest.raises(OwnershipConflict):
            jobs.put(_job(workspace_id="ws-b"))
        assert not jobs.put_if_worker_owned(
            _job(org_id="org-b", task="Stolen"), worker_id="worker-a", attempts=1
        )

        stored = jobs.get(original.job_id)
        assert (stored.org_id, stored.workspace_id, stored.task) == (
            "org-a",
            "ws-a",
            "Run",
        )
        assert jobs.put_if_worker_owned(
            _job(task="Updated"), worker_id="worker-a", attempts=1
        )
        assert jobs.put(_job(task="Replaced")).task == "Replaced"
    finally:
        store.close()
