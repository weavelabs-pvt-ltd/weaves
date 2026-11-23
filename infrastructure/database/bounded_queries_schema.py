"""Migration 28: indexes that replace whole-collection scans (Phase 5.4).

Every index backs one bounded query added in the same slice and is asserted by
an ``EXPLAIN`` test (``tests/integration/test_bounded_queries_postgres.py``).
No table is added: a recovery or dispatch tick reads rows by the status it is
looking for, and a processed row leaves that status, so the partial indexes are
themselves the checkpoint. Every statement is idempotent.
"""

from __future__ import annotations

BOUNDED_QUERIES_MIGRATION: tuple[int, tuple[str, ...]] = (
    28,
    (
        # Recovery: stale ``running``/``executing`` rows by lease age.
        """CREATE INDEX IF NOT EXISTS workflow_runs_running_idx
               ON product_records (updated_at, record_id)
               WHERE collection = 'workflow_runs'
                 AND payload->>'status' = 'running'""",
        """CREATE INDEX IF NOT EXISTS execution_jobs_leased_idx
               ON product_records (updated_at, record_id)
               WHERE collection = 'execution_jobs'
                 AND payload->>'status' IN ('running', 'cancel_requested')""",
        """CREATE INDEX IF NOT EXISTS tool_invocations_executing_idx
               ON product_records (updated_at, record_id)
               WHERE collection = 'tool_invocations'
                 AND payload->>'status' = 'executing'""",
        # Studio dispatch: active canvas runs and due calendar schedules.
        """CREATE INDEX IF NOT EXISTS canvas_runs_active_idx
               ON product_records (record_id)
               WHERE collection = 'canvas_runs'
                 AND payload->>'status' IN ('queued', 'running')""",
        """CREATE INDEX IF NOT EXISTS canvas_settings_due_idx
               ON product_records ((payload->>'next_run_at'), record_id)
               WHERE collection = 'canvas_settings'
                 AND payload->>'sched' = 'true'
                 AND payload->>'next_run_at' IS NOT NULL""",
        # Point lookups that used to filter a full tenant list in Python.
        """CREATE INDEX IF NOT EXISTS agent_runs_run_idx
               ON product_records (org_id, workspace_id, (payload->>'run_id'))
               WHERE collection = 'agent_runs'""",
        """CREATE INDEX IF NOT EXISTS task_turns_task_idx
               ON product_records (org_id, workspace_id, (payload->>'task_id'))
               WHERE collection = 'task_turns'""",
        """CREATE INDEX IF NOT EXISTS tool_invocations_approval_idx
               ON product_records
                 (org_id, workspace_id, (payload->>'approval_request_id'))
               WHERE collection = 'tool_invocations'
                 AND payload->>'approval_request_id' IS NOT NULL""",
        """CREATE INDEX IF NOT EXISTS artifacts_run_idx
               ON product_records (org_id, workspace_id, (payload->>'run_id'))
               WHERE collection = 'artifacts'""",
        """CREATE INDEX IF NOT EXISTS tool_invocations_run_idx
               ON product_records (org_id, workspace_id, (payload->>'run_id'))
               WHERE collection = 'tool_invocations'""",
        """CREATE INDEX IF NOT EXISTS audit_events_action_target_idx
               ON product_records
                 (org_id, (payload->>'action'), (payload->>'target_id'))
               WHERE collection = 'audit_events'""",
        # Newest-first pages and pending counts.
        """CREATE INDEX IF NOT EXISTS approval_requests_page_idx
               ON product_records
                 (org_id, workspace_id, created_at DESC, record_id DESC)
               WHERE collection = 'approval_requests'""",
        """CREATE INDEX IF NOT EXISTS approval_requests_pending_idx
               ON product_records (org_id, workspace_id)
               WHERE collection = 'approval_requests'
                 AND payload->>'status' = 'pending'""",
        """CREATE INDEX IF NOT EXISTS evaluation_executions_page_idx
               ON product_records
                 (org_id, workspace_id, created_at DESC, record_id DESC)
               WHERE collection = 'evaluation_executions'""",
        """CREATE INDEX IF NOT EXISTS evaluation_reports_page_idx
               ON product_records
                 (org_id, workspace_id, created_at DESC, record_id DESC)
               WHERE collection = 'evaluation_reports'""",
        """CREATE INDEX IF NOT EXISTS canvas_runs_workflow_page_idx
               ON product_records
                 (org_id, workspace_id, (payload->>'workflow_id'),
                  created_at DESC, record_id DESC)
               WHERE collection = 'canvas_runs'""",
        # Home overview counts.
        """CREATE INDEX IF NOT EXISTS tasks_status_idx
               ON product_records (org_id, workspace_id, (payload->>'status'))
               WHERE collection = 'tasks'""",
        """CREATE INDEX IF NOT EXISTS task_turns_active_idx
               ON product_records (org_id, workspace_id)
               WHERE collection = 'task_turns'
                 AND payload->>'status' IN ('queued', 'running')""",
        """CREATE INDEX IF NOT EXISTS task_turns_recent_idx
               ON product_records (org_id, workspace_id, created_at)
               WHERE collection = 'task_turns'""",
    ),
)
