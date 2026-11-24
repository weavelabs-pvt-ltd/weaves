"""Round-robin, per-organization-capped claiming of queued work (PostgreSQL).

The claim runs in one transaction and never uses a stored counter:

1. enumerate organizations that have queued rows in this collection, least
   recently claimed first (``tenant_scheduler_state``; never-claimed first);
2. for each, try ``pg_try_advisory_xact_lock`` on the organization. The lock is
   shared by both queues, so two slots (or processes) can never both pass the
   cap check for the same organization; a contender simply moves to the next;
3. count the organization's running rows in *both* queues from the partial
   indexes (so a crash can never leak capacity: a dead worker's rows leave
   ``running`` through lease recovery and stop counting);
4. when under the cap (or when the cap is only observed), claim that
   organization's oldest queued row with ``FOR UPDATE SKIP LOCKED`` and record
   ``last_claimed_at`` in the same transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from application.ports.fair_claim import FairClaim
from application.ports.limits import LimitPolicy
from contracts.v1.base import ProductContract

# Bounds one claim attempt however many organizations have a backlog.
MAX_CANDIDATE_ORGANIZATIONS = 200
_LOCK_PREFIX = "weaves:fair-claim:"

_CANDIDATES = """SELECT q.org_id FROM (
           SELECT DISTINCT org_id FROM product_records
           WHERE collection = %s AND payload->>'status' = 'queued'
             AND org_id IS NOT NULL) q
       LEFT JOIN tenant_scheduler_state s ON s.org_id = q.org_id
       ORDER BY s.last_claimed_at NULLS FIRST, q.org_id
       LIMIT %s"""

# Running work of one organization across both queues.
RUNNING_COUNT = """SELECT
         (SELECT count(*) FROM product_records
           WHERE collection = 'execution_jobs' AND org_id = %s
             AND payload->>'status' IN ('running', 'cancel_requested'))
       + (SELECT count(*) FROM product_records
           WHERE collection = 'task_turns' AND org_id = %s
             AND payload->>'status' = 'running')"""

_CLAIM = """WITH candidate AS (
           SELECT record_id FROM product_records
           WHERE collection = %s AND org_id = %s
             AND payload->>'status' = 'queued'
           ORDER BY created_at, record_id
           LIMIT 1 FOR UPDATE SKIP LOCKED
       )
       UPDATE product_records AS job
       SET payload = job.payload || jsonb_build_object(
               'status', 'running',
               'attempts',
               COALESCE((job.payload->>'attempts')::integer, 0) + 1,
               'worker_id', %s::text,
               'started_at', to_jsonb(%s::timestamptz),
               'updated_at', to_jsonb(%s::timestamptz)
           ),
           updated_at = %s
       FROM candidate
       WHERE job.collection = %s AND job.record_id = candidate.record_id
       RETURNING job.payload"""

_MARK = """INSERT INTO tenant_scheduler_state (org_id, last_claimed_at)
       VALUES (%s, %s)
       ON CONFLICT (org_id) DO UPDATE SET last_claimed_at = EXCLUDED.last_claimed_at"""


class PostgresFairClaimMixin[TContract: ProductContract]:
    """Fair claim and queue-age reads for the two queued collections."""

    _pool: Any
    _collection: str

    def _deserialize(self, payload: object) -> TContract:
        """Deserialize one repository payload into its declared contract."""
        raise NotImplementedError

    def claim_next_fair(
        self, worker_id: str, timestamp: datetime, limits: LimitPolicy
    ) -> FairClaim[TContract] | None:
        """Claim the next row for the least recently served eligible organization."""
        with self._pool.connection() as connection:
            orgs = [
                str(row[0])
                for row in connection.execute(
                    _CANDIDATES, (self._collection, MAX_CANDIDATE_ORGANIZATIONS)
                ).fetchall()
            ]
            for org_id in orgs:
                got = connection.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_LOCK_PREFIX + org_id,),
                ).fetchone()
                if not got or not got[0]:
                    continue
                org_limits = limits.for_org(org_id)
                # A new statement, so it sees work other slots committed
                # before they released this organization's lock.
                row = connection.execute(RUNNING_COUNT, (org_id, org_id)).fetchone()
                running = int(row[0]) if row else 0
                cap = org_limits.max_running_per_org
                if org_limits.enforcing and running >= cap:
                    continue
                claimed = connection.execute(
                    _CLAIM,
                    (
                        self._collection,
                        org_id,
                        worker_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        self._collection,
                    ),
                ).fetchone()
                if claimed is None:
                    continue
                connection.execute(_MARK, (org_id, timestamp))
                return FairClaim(
                    self._deserialize(claimed[0]), running, cap, org_limits.mode
                )
        return None

    def oldest_queued_at(self) -> datetime | None:
        """Return the creation time of the oldest queued row, or None."""
        with self._pool.connection() as connection:
            row = connection.execute(
                """SELECT min(created_at) FROM (
                       SELECT created_at FROM product_records
                       WHERE collection = %s AND payload->>'status' = 'queued'
                       ORDER BY created_at LIMIT 1) oldest""",
                (self._collection,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def prune_claim_state(self, claimed_before: datetime) -> int:
        """Delete round-robin history older than ``claimed_before``."""
        with self._pool.connection() as connection:
            result = connection.execute(
                "DELETE FROM tenant_scheduler_state WHERE last_claimed_at < %s",
                (claimed_before,),
            )
        return int(result.rowcount)
