"""Global queue gauges read with one bounded aggregate query.

The result is deliberately not per tenant: tenant names never become metric
labels, and per-tenant queue numbers come from SQL reports instead.
"""

from __future__ import annotations

from typing import Any

from shared.metrics import OLDEST_QUEUED_AGE, QUEUE_DEPTH, RUNNING
from shared.request_context import system_scope

QUEUES = {"execution_jobs": "execution", "task_turns": "task"}


def publish_queue_gauges(store: Any) -> None:
    """Set queue depth, running count and oldest queued age for each queue."""
    with system_scope("queue"), store.connection() as connection:
        rows = connection.execute(
            """SELECT collection, payload->>'status', count(*),
                      COALESCE(extract(epoch FROM now() - min(created_at)), 0)
               FROM product_records
               WHERE collection IN ('execution_jobs', 'task_turns')
                 AND payload->>'status' IN ('queued', 'running')
               GROUP BY collection, payload->>'status'"""
        ).fetchall()
    seen = {(row[0], row[1]): (row[2], row[3]) for row in rows}
    for collection, queue in QUEUES.items():
        depth, age = seen.get((collection, "queued"), (0, 0))
        QUEUE_DEPTH.set(float(depth), queue=queue)
        OLDEST_QUEUED_AGE.set(float(age) if depth else 0.0, queue=queue)
        running = seen.get((collection, "running"), (0, 0))[0]
        RUNNING.set(float(running), queue=queue)
