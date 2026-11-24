"""Migration 27: the per-organization usage ledger and its daily rollup.

A deliberate exception to the one-JSONB-table rule (``docs/DATA_MODEL.md``): the
ledger is append-heavy, is summed and grouped by organization, agent and day,
and needs a real unique key for idempotent settlement. Both tables carry
``org_id`` and the same restricted-role row policy as ``product_secrets``: a
tenant sees only its own rows, and the named system purpose ``usage`` (the
reconciler, retention and the operator report) sees all of them. Every
statement is idempotent. Money is integer nano-USD, never a float.
"""

from __future__ import annotations

from infrastructure.database.row_security import APP_ROLE_GRANTS, POLICY_FUNCTIONS

USAGE_POLICY = (
    "org_id = nullif(current_setting('app.org_id', true), '') "
    "OR nullif(current_setting('app.system_purpose', true), '') = 'usage'"
)

USAGE_MIGRATION: tuple[int, tuple[str, ...]] = (
    27,
    (
        # The ``usage`` purpose reads the runs and jobs it reconciles against.
        *POLICY_FUNCTIONS,
        """CREATE TABLE IF NOT EXISTS usage_ledger (
               org_id text NOT NULL,
               reservation_key text NOT NULL,
               workspace_id text NOT NULL,
               agent_run_id text NOT NULL,
               run_id text NOT NULL,
               agent_id text NOT NULL,
               provider_id text,
               model_id text,
               state text NOT NULL
                   CHECK (state IN ('reserved', 'settled', 'released', 'unknown')),
               reserved_input_tokens bigint NOT NULL CHECK (reserved_input_tokens >= 0),
               reserved_output_tokens bigint NOT NULL
                   CHECK (reserved_output_tokens >= 0),
               reserved_cost_nano_usd bigint CHECK (reserved_cost_nano_usd >= 0),
               input_tokens bigint CHECK (input_tokens >= 0),
               output_tokens bigint CHECK (output_tokens >= 0),
               estimated_cost_nano_usd bigint CHECK (estimated_cost_nano_usd >= 0),
               outcome text,
               reserved_at timestamptz NOT NULL,
               settled_at timestamptz,
               updated_at timestamptz NOT NULL,
               PRIMARY KEY (org_id, reservation_key),
               CHECK (state <> 'settled' OR (input_tokens IS NOT NULL
                      AND output_tokens IS NOT NULL AND settled_at IS NOT NULL))
           )""",
        """CREATE INDEX IF NOT EXISTS usage_ledger_reserved_idx
               ON usage_ledger (reserved_at) WHERE state = 'reserved'""",
        """CREATE INDEX IF NOT EXISTS usage_ledger_outstanding_idx
               ON usage_ledger (org_id, reserved_at)
               WHERE state IN ('reserved', 'unknown')""",
        """CREATE INDEX IF NOT EXISTS usage_ledger_org_time_idx
               ON usage_ledger (org_id, reserved_at)""",
        """CREATE TABLE IF NOT EXISTS usage_daily (
               org_id text NOT NULL,
               day date NOT NULL,
               agent_id text NOT NULL,
               provider_id text NOT NULL,
               model_id text NOT NULL,
               calls bigint NOT NULL DEFAULT 0,
               input_tokens bigint NOT NULL DEFAULT 0,
               output_tokens bigint NOT NULL DEFAULT 0,
               estimated_cost_nano_usd bigint NOT NULL DEFAULT 0,
               unpriced_calls bigint NOT NULL DEFAULT 0,
               PRIMARY KEY (org_id, day, agent_id, provider_id, model_id)
           )""",
        "CREATE INDEX IF NOT EXISTS usage_daily_day_idx ON usage_daily (day)",
        "ALTER TABLE usage_ledger ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE usage_daily ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS tenant_isolation ON usage_ledger",
        "DROP POLICY IF EXISTS tenant_isolation ON usage_daily",
        f"""CREATE POLICY tenant_isolation ON usage_ledger
               USING ({USAGE_POLICY}) WITH CHECK ({USAGE_POLICY})""",
        f"""CREATE POLICY tenant_isolation ON usage_daily
               USING ({USAGE_POLICY}) WITH CHECK ({USAGE_POLICY})""",
        APP_ROLE_GRANTS,
    ),
)
