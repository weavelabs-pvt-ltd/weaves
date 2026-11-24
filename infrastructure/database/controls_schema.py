"""Migration 30: platform controls, the operator audit trail and quarantines.

Three small tables outside ``product_records`` (the rows are platform-wide or
operator-owned, not tenant content; see ``docs/DATA_MODEL.md``):

* ``platform_controls``: one row (``id = 1``) of runtime switches. A NULL value
  means "use the environment default", so a fresh database changes nothing.
  The restricted application role may only read it.
* ``operator_actions``: append-only record of every operator command. Updates,
  deletes and truncation are rejected by triggers, even for the table owner.
  The restricted role has no access at all.
* ``org_quarantines``: exactly what a tenant quarantine changed, so a release
  reverses only that. Operator-only, like the audit table.

Every statement is idempotent. Owner connections (operator scripts, migrations)
bypass row security as for every other table.
"""

from __future__ import annotations

from infrastructure.database.row_security import APP_ROLE_GRANTS

CONTROLS_MIGRATION: tuple[int, tuple[str, ...]] = (
    30,
    (
        """CREATE TABLE IF NOT EXISTS platform_controls (
               id smallint PRIMARY KEY CHECK (id = 1),
               version bigint NOT NULL DEFAULT 0,
               admissions_paused boolean,
               worker_paused boolean,
               read_only boolean,
               signup_enabled boolean,
               message text,
               updated_by text,
               reason text,
               updated_at timestamptz NOT NULL DEFAULT now()
           )""",
        "INSERT INTO platform_controls (id) VALUES (1) ON CONFLICT DO NOTHING",
        """CREATE TABLE IF NOT EXISTS operator_actions (
               action_id text PRIMARY KEY,
               at timestamptz NOT NULL DEFAULT now(),
               operator text NOT NULL,
               command text NOT NULL,
               target text NOT NULL DEFAULT '',
               org_id text,
               reason text NOT NULL,
               ticket text,
               correlation_id text NOT NULL,
               result text NOT NULL,
               details jsonb NOT NULL DEFAULT '{}'::jsonb
           )""",
        """CREATE INDEX IF NOT EXISTS operator_actions_at_idx
               ON operator_actions (at DESC)""",
        """CREATE INDEX IF NOT EXISTS operator_actions_org_idx
               ON operator_actions (org_id, at DESC) WHERE org_id IS NOT NULL""",
        """CREATE OR REPLACE FUNCTION operator_actions_append_only()
               RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                 RAISE EXCEPTION 'operator_actions is append-only';
               END $$""",
        "DROP TRIGGER IF EXISTS operator_actions_no_change ON operator_actions",
        """CREATE TRIGGER operator_actions_no_change
               BEFORE UPDATE OR DELETE ON operator_actions
               FOR EACH ROW EXECUTE FUNCTION operator_actions_append_only()""",
        "DROP TRIGGER IF EXISTS operator_actions_no_truncate ON operator_actions",
        """CREATE TRIGGER operator_actions_no_truncate
               BEFORE TRUNCATE ON operator_actions
               FOR EACH STATEMENT EXECUTE FUNCTION operator_actions_append_only()""",
        """CREATE TABLE IF NOT EXISTS org_quarantines (
               org_id text PRIMARY KEY,
               state text NOT NULL CHECK (state IN ('quarantined', 'released')),
               changes jsonb NOT NULL DEFAULT '{}'::jsonb,
               quarantined_by text NOT NULL,
               reason text NOT NULL,
               correlation_id text NOT NULL,
               quarantined_at timestamptz NOT NULL,
               released_by text,
               released_at timestamptz,
               release_reason text
           )""",
        "ALTER TABLE platform_controls ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE operator_actions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE org_quarantines ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS controls_read ON platform_controls",
        # Read-only for everyone; no write policy exists, so the restricted role
        # cannot change a switch. The other two tables have no policy at all.
        """CREATE POLICY controls_read ON platform_controls
               FOR SELECT USING (true)""",
        APP_ROLE_GRANTS,
    ),
)
