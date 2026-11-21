"""Row-level security schema: restricted role, tenant policies and grants."""

from infrastructure.database.account_schema import ACCOUNT_SCHEMA

# Grants the restricted application role DML on every current table. Re-run
# whenever a later step creates a table (secrets, catalog ownership).
APP_ROLE_GRANTS = """DO $$
               BEGIN
                 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'weaves_app') THEN
                   EXECUTE format('GRANT USAGE ON SCHEMA %I TO weaves_app',
                                  current_schema());
                   EXECUTE format(
                     'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I
                      TO weaves_app', current_schema());
                 END IF;
               EXCEPTION WHEN insufficient_privilege THEN
                 RAISE NOTICE 'weaves_app grants skipped';
               END $$"""


# Re-applied on every start (CREATE OR REPLACE is idempotent), so policy changes
# ship without a schema version and an older release can still roll back.
POLICY_FUNCTIONS: tuple[str, ...] = (
    """CREATE OR REPLACE FUNCTION weaves_system_allows(coll text)
           RETURNS boolean LANGUAGE sql STABLE AS $$
           SELECT CASE nullif(current_setting('app.system_purpose', true), '')
             WHEN 'bootstrap' THEN true
             WHEN 'auth' THEN coll = ANY (ARRAY[
               'organizations', 'workspaces', 'users', 'principals',
               'api_credentials', 'roles', 'role_bindings',
               'audit_events', 'accounts', 'account_identities',
               'memberships', 'account_oauth_states',
               'account_sessions'])
             WHEN 'queue' THEN coll = ANY (ARRAY[
               'execution_jobs', 'task_turns', 'tasks', 'task_messages',
               'evaluation_executions', 'workflow_runs', 'agent_runs',
               'audit_events', 'organizations', 'workspaces'])
             WHEN 'dispatch' THEN coll = ANY (ARRAY[
               'workflow_schedules', 'workflow_triggers', 'execution_jobs',
               'organizations', 'workspaces', 'principals', 'users',
               'roles', 'role_bindings', 'audit_events'])
             WHEN 'recovery' THEN coll = ANY (ARRAY[
               'execution_jobs', 'task_turns', 'tasks', 'task_messages',
               'workflow_runs', 'agent_runs', 'tool_invocations',
               'approval_requests', 'artifacts', 'audit_events',
               'evaluation_executions', 'organizations', 'workspaces'])
             WHEN 'studio' THEN coll = ANY (ARRAY[
               'canvas_settings', 'canvas_runs'])
             WHEN 'account' THEN coll = ANY (ARRAY[
               'accounts', 'account_identities', 'memberships',
               'account_oauth_states', 'account_sessions',
               'account_organization_requests',
               'organizations', 'workspaces', 'users', 'principals'])
             WHEN 'catalog' THEN coll = ANY (ARRAY[
               'plugins', 'plugin_installations'])
             WHEN 'oauth' THEN coll = ANY (ARRAY[
               'oauth_authorization_states', 'organizations', 'workspaces',
               'principals', 'users', 'roles', 'role_bindings'])
             ELSE false
           END
           $$""",
    """CREATE OR REPLACE FUNCTION weaves_rls_allows(
               coll text, rid text, row_org text)
           RETURNS boolean LANGUAGE sql STABLE AS $$
           SELECT CASE
             WHEN coll = 'organizations'
               THEN rid = nullif(current_setting('app.org_id', true), '')
                    OR weaves_system_allows(coll)
             WHEN coll IN ('accounts', 'account_identities',
                         'account_oauth_states', 'account_sessions',
                         'account_organization_requests')
               THEN weaves_system_allows(coll)
             WHEN row_org IS NULL THEN true
             ELSE row_org = nullif(current_setting('app.org_id', true), '')
                  OR weaves_system_allows(coll)
           END
           $$""",
)


ROW_SECURITY_MIGRATION: tuple[int, tuple[str, ...]] = (
    21,
    (
        """DO $$
           BEGIN
             IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'weaves_app')
             THEN
               CREATE ROLE weaves_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
             END IF;
           EXCEPTION WHEN insufficient_privilege THEN
             RAISE NOTICE 'weaves_app role not created; row policies stay unused';
           END $$""",
        *POLICY_FUNCTIONS,
        "ALTER TABLE product_records ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS tenant_isolation ON product_records",
        """CREATE POLICY tenant_isolation ON product_records
               USING (weaves_rls_allows(collection, record_id, org_id))
               WITH CHECK (weaves_rls_allows(collection, record_id, org_id))""",
        APP_ROLE_GRANTS,
    ),
)


REPEATABLE_STATEMENTS: tuple[str, ...] = (*POLICY_FUNCTIONS, *ACCOUNT_SCHEMA)
