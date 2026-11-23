"""Migration 26: fair claiming state and the per-organization queue indexes.

``tenant_scheduler_state`` holds one row per organization that recently had
work claimed: just the organization id and the instant of its last claim, used
to serve organizations round robin. It carries no tenant content and nothing
counts against it (running capacity is always derived by counting running rows),
so a lost or pruned row only changes who is served first. The worker prunes
rows older than a day. Row security admits only the ``queue`` system purpose,
the one cross-tenant operation that reads or writes it.

The four partial indexes serve the claim: an organization's oldest queued row
and the count of an organization's running rows, per queue. The older global
``(created_at, record_id)`` queued indexes stay for the unfair fallback claim
(``WEAVES_FAIR_CLAIM=off``). Every statement is idempotent.
"""

from __future__ import annotations

from infrastructure.database.row_security import APP_ROLE_GRANTS

SCHEDULER_MIGRATION: tuple[int, tuple[str, ...]] = (
    26,
    (
        """CREATE TABLE IF NOT EXISTS tenant_scheduler_state (
               org_id text PRIMARY KEY,
               last_claimed_at timestamptz NOT NULL
           )""",
        """CREATE INDEX IF NOT EXISTS tenant_scheduler_state_claimed_idx
               ON tenant_scheduler_state (last_claimed_at)""",
        "ALTER TABLE tenant_scheduler_state ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS queue_only ON tenant_scheduler_state",
        """CREATE POLICY queue_only ON tenant_scheduler_state
               USING (nullif(current_setting('app.system_purpose', true), '')
                      = 'queue')
               WITH CHECK (nullif(current_setting('app.system_purpose', true), '')
                      = 'queue')""",
        APP_ROLE_GRANTS,
        """CREATE INDEX IF NOT EXISTS product_records_execution_job_org_queued_idx
               ON product_records (org_id, created_at, record_id)
               WHERE collection = 'execution_jobs'
                 AND payload->>'status' = 'queued'""",
        """CREATE INDEX IF NOT EXISTS product_records_task_turn_org_queued_idx
               ON product_records (org_id, created_at, record_id)
               WHERE collection = 'task_turns'
                 AND payload->>'status' = 'queued'""",
        """CREATE INDEX IF NOT EXISTS product_records_execution_job_org_running_idx
               ON product_records (org_id)
               WHERE collection = 'execution_jobs'
                 AND payload->>'status' IN ('running', 'cancel_requested')""",
        """CREATE INDEX IF NOT EXISTS product_records_task_turn_org_running_idx
               ON product_records (org_id)
               WHERE collection = 'task_turns'
                 AND payload->>'status' = 'running'""",
    ),
)
