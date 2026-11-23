"""Migration 25: count indexes and the global queue counter for tenant limits.

Admission counts the stored rows inside the inserting transaction (see
``modules/limits/service.py``), so the counts must stay cheap as history grows:
queued work and tool calls get partial indexes led by ``org_id``. The other
counted collections (users, schedules, agents, workflows, tasks) are bounded by
the limits themselves and are served by ``product_records_org_scope_idx``.

``weaves_global_queued_work`` lets the restricted application role learn one
number across organizations (queued work for the emergency brake) without any
row access: it runs as the migration owner and returns only a count. Every
statement is idempotent.
"""

from __future__ import annotations

LIMITS_MIGRATION: tuple[int, tuple[str, ...]] = (
    25,
    (
        """CREATE INDEX IF NOT EXISTS limits_turn_queued_org_idx
               ON product_records (org_id)
               WHERE collection = 'task_turns'
                 AND payload->>'status' = 'queued'""",
        """CREATE INDEX IF NOT EXISTS limits_job_queued_org_idx
               ON product_records (org_id)
               WHERE collection = 'execution_jobs'
                 AND payload->>'status' = 'queued'""",
        """CREATE INDEX IF NOT EXISTS limits_tool_invocation_org_idx
               ON product_records (org_id, created_at)
               WHERE collection = 'tool_invocations'""",
        """CREATE OR REPLACE FUNCTION weaves_global_queued_work()
               RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER
               SET search_path FROM CURRENT AS $$
               SELECT (SELECT count(*) FROM product_records
                        WHERE collection = 'task_turns'
                          AND payload->>'status' = 'queued')
                    + (SELECT count(*) FROM product_records
                        WHERE collection = 'execution_jobs'
                          AND payload->>'status' = 'queued')
               $$""",
    ),
)
