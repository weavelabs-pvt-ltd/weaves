# Physical schema

Introspected from a real database: a scratch database was created on a disposable
PostgreSQL server, `ProductPostgresStore(url)` (the repository's own migration
runner) was run, then `EncryptedPostgresSecretStore` (creates `product_secrets`) and
`activate_ownership` (migration 20), and the catalogs were read through
`information_schema`, `pg_indexes`, `pg_trigger`, `pg_policies`, `pg_proc` and
`pg_constraint`. Index definitions in [generated.md](generated.md) section 3 are parsed
from the migration source instead; both agree. The handwritten parts below name the
query each object serves, found by reading the repositories.

Snapshot basis: `origin/main` commit `b2b2751`, schema versions applied by a fresh start-up
`1-19, 21` (see 6). Version 20 appears only after an operator runs the activation.
Treat the physical dump below as a snapshot; the generated file is the source that is
kept current.

## 1. Tables and columns

### `product_records`

| # | Column | Type | Null | Default | Notes |
|---|---|---|---|---|---|
| 1 | `collection` | text | no | | logical entity type; part of primary key |
| 2 | `record_id` | text | no | | contract id (or `org:id` for tenant-keyed collections); part of primary key |
| 3 | `org_id` | text | yes | | tenant scope; NULL for global rows |
| 4 | `workspace_id` | text | yes | | workspace scope |
| 5 | `idempotency_key` | text | yes | | only set for run, job, task and task-turn requests |
| 6 | `requested_by_principal_id` | text | yes | | idempotency scope |
| 7 | `payload` | jsonb | no | | the whole contract (`model_dump(mode="json")`) |
| 8 | `created_at` | timestamptz | no | `now()` | set from the payload's `created_at` by `create()` (COALESCE) |
| 9 | `updated_at` | timestamptz | no | `now()` | `now()` on `put`; application value on conditional updates |

Constraint: `product_records_pkey PRIMARY KEY (collection, record_id)`. No other
constraints, no foreign keys, no check constraints. Row-level security enabled
(not forced). Triggers: none, except after migration 20:
`plugin_record_guard`, `plugin_record_owner`, `plugin_installation_owner_guard`.

### `product_secrets`

Created by `EncryptedPostgresSecretStore.__init__` (`infrastructure/secrets/store.py`)
when an encryption key is configured, not by a numbered migration. Starts as the four
original columns, with the rest added by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `secret_ref` | text | no | | primary key, `secret:{uuid}` |
| `ciphertext` | bytea | no | | Fernet token |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` | |
| `org_id` | text | yes | | owner organization; NULL means unowned and unreadable |
| `workspace_id` | text | yes | | |
| `purpose` | text | yes | | `model_provider`, `connector`, `oauth_setup`, `unspecified` |
| `owner_ref` | text | yes | | `collection:record_id` of the owning record |
| `key_id` | text | yes | | first 12 hex of SHA-256 of the encryption key |

Index `product_secrets_pkey (secret_ref)`, `product_secrets_org_idx (org_id)`.
At every start the store also runs a one-off style backfill (`_BACKFILL_OWNERSHIP`)
that sets `org_id` on unowned secrets when exactly one organization references them
through `auth_ref` / `secret_ref`.

### `product_login_throttle`

| Column | Type | Null | Notes |
|---|---|---|---|
| `bucket_key` | text | no | primary key; hashed bucket name (see `login_bucket_key`) |
| `window_started_at` | timestamptz | no | |
| `attempts` | integer | no | `CHECK (attempts > 0)` |
| `updated_at` | timestamptz | no | |

Index `product_login_throttle_window_idx (window_started_at)`.

### `tenant_scheduler_state` (migration 26)

`org_id text PRIMARY KEY`, `last_claimed_at timestamptz NOT NULL`, index
`tenant_scheduler_state_claimed_idx (last_claimed_at)`. One row per organization that
recently had work claimed; the worker orders organizations by it (never claimed first)
so a backlogged tenant cannot starve another. Row security allows only the `queue`
system purpose. Rows older than a day are pruned by the worker; capacity is never
stored, it is derived by counting running rows. Four partial indexes on `product_records`
serve the claim: `product_records_{execution_job,task_turn}_org_queued_idx
(org_id, created_at, record_id)` and `product_records_{execution_job,task_turn}_org_running_idx (org_id)`.

### `product_schema_migrations`

`version integer PRIMARY KEY`, `applied_at timestamptz NOT NULL DEFAULT now()`.

### `product_plugin_ownership` (after migration 20)

| Column | Type | Null | Default |
|---|---|---|---|
| `plugin_id` | text | no | primary key |
| `record_collection` | text | no | `'plugins'`, `CHECK (record_collection = 'plugins')` |
| `state` | text | no | `CHECK (state IN ('builtin','owned','quarantined'))` |
| `org_id` | text | yes | |
| `reason` | text | yes | |
| `source_org_ids` | text[] | no | |
| `created_at` | timestamptz | no | `now()` |

Constraints: `FOREIGN KEY (record_collection, plugin_id) REFERENCES
product_records (collection, record_id) ON DELETE CASCADE` (the only foreign key in
the schema) and `CHECK ((state='owned' AND org_id IS NOT NULL AND reason IS NULL) OR
(state='builtin' AND org_id IS NULL AND reason IS NULL) OR (state='quarantined' AND
org_id IS NULL AND reason IS NOT NULL))`. Index `product_plugin_owner_org_idx
(org_id, plugin_id) WHERE state='owned'`.

### `product_plugin_ownership_rollout` (after migration 20)

`version integer PRIMARY KEY CHECK (version = 20)`, `reader_image text NOT NULL`,
`counts jsonb NOT NULL`, `applied_at timestamptz NOT NULL DEFAULT now()`. Immutable
rollout evidence (trigger `plugin_rollout_guard` rejects update and delete).

## 2. Indexes and the queries they serve

All indexes below are on `product_records` unless stated. "uq" = unique. Partial
predicates are shown in [generated.md](generated.md) section 3 and repeated in the
physical definitions by `pg_indexes`. The query column names the repository method
(`infrastructure/persistence/postgres_repository.py` = PR,
`postgres_query_repository.py` = PQ) and a call site.

| Index | Key | Serves |
|---|---|---|
| `product_records_pkey` | `(collection, record_id)` | `get`, `put` upsert conflict target, `delete`, `consume`, `list` (range scan of one collection ordered by `record_id`) in PR |
| `product_records_org_scope_idx` | `(collection, org_id, record_id)` | `get_scoped`, `list_scoped(org)`, bounded export `page`, purge `delete_batch` (`organization_data.py`) |
| `product_records_workspace_scope_idx` | `(collection, org_id, workspace_id, record_id)` | `get_scoped` and `list_scoped` with a workspace |
| `product_records_run_idempotency_uq` | `(org_id, workspace_id, requested_by_principal_id, idempotency_key)` where `workflow_runs` and key not null | uniqueness of run requests; `find_by_idempotency_key` for `workflow_runs` |
| `product_records_execution_job_idempotency_uq` | same four columns where `execution_jobs` | same for jobs |
| `product_records_task_idempotency_uq` | same where `tasks` | `modules/tasks/creation_persistence.py::find_existing` |
| `product_records_task_turn_idempotency_uq` | same where `task_turns` | uniqueness of turn requests |
| `product_records_api_token_digest_uq` | `(collection, payload->>'token_digest')` where `api_credentials` | `authentication_service.py`: `find_by_field("token_digest")` |
| `product_records_role_binding_scope_uq` | `(org_id, principal_id, role_id, COALESCE(workspace_id,''))` where `role_bindings` | uniqueness only (no lookup uses it directly; `list_scoped` serves reads) |
| `product_records_execution_job_claim_idx` | `(created_at, record_id)` where `execution_jobs` and `status='queued'` | `claim_next_queued` for jobs (`ORDER BY created_at, record_id LIMIT 1 FOR UPDATE SKIP LOCKED`, PQ) |
| `product_records_active_user_email_uq` | `(org_id, lower(payload->>'email'))` where `users` and status not `deleted` | uniqueness of active email per organization |
| `product_records_workflow_trigger_secret_uq` | `(collection, payload->>'secret_digest')` where `workflow_triggers` | uniqueness only; webhook auth reads the trigger by id then compares the digest (`runtime_triggers.py`) |
| `product_records_workflow_schedule_due_idx` | `(payload->>'next_run_at', record_id)` where `workflow_schedules` and status active | `list_due` (PR) used by `runtime_schedules.py` |
| `product_records_audit_event_org_page_idx` | `(org_id, created_at DESC, record_id DESC)` where `audit_events` | `list_audit_page` without workspace (PQ) |
| `product_records_audit_event_workspace_page_idx` | `(org_id, workspace_id, created_at DESC, record_id DESC)` where `audit_events` | `list_audit_page` with workspace |
| `product_records_system_role_key_uq` | `(org_id, payload->>'system_role_key')` where `roles` and key not null | uniqueness of the admin/builder/user roles per organization |
| `product_records_task_page_idx` | `(org_id, workspace_id, created_at DESC, record_id DESC)` where `tasks` | `list_task_page` (PQ) |
| `product_records_task_messages_page_idx` | `(org_id, workspace_id, payload->>'task_id', created_at DESC, record_id DESC)` where `task_messages` | `list_task_messages_page` (PQ) |
| `product_records_task_turn_claim_idx` | `(created_at, record_id)` where `task_turns` and status queued | `claim_next_queued` for turns (`modules/tasks/worker_service.py`) |
| `product_records_task_turn_stale_idx` | `(updated_at, record_id)` where `task_turns` and status running | `list_status_updated_before` for turns (`recover_stale_turns`) |
| `skills_org_slug_unique_idx`, `skills_personal_slug_unique_idx` | `(org_id, slug)` / `(org_id, owner_principal_id, slug)` by `scope` | uniqueness of skill slugs |
| `skills_version_number_unique_idx` | `(org_id, skill_id, version)` | uniqueness of version numbers |
| `skills_publication_version_unique_idx`, `skills_revocation_version_unique_idx` | `(org_id, skill_version_id)` | one publication and one revocation per version |
| `account_identity_verified_uq` | `(kind, COALESCE(issuer,''), subject)` where `account_identities` and verified | one account per verified identity, platform-wide |
| `account_identity_owner_uq` | `(account_id, kind, COALESCE(issuer,''), subject)` | an account holds an identity once; also serves `list_by_field("account_id")` (its first column) |
| `account_identity_account_idx` | `(account_id)` | `list_by_field("account_id")` (account overview, quota email lookup). Redundant with the leading column of `account_identity_owner_uq` |
| `account_identity_subject_idx` | `(subject)` | `find_by_field` / `list_by_field("subject")` in `account_sign_in.py` |
| `membership_account_org_uq`, `membership_user_uq` | `(org_id, account_id)`, `(org_id, user_id)` where `memberships` | uniqueness |
| `membership_account_idx` | `(account_id)` | `list_by_field("account_id")` in selection, overview, linking, creation replay |
| `membership_user_idx` | `(user_id)` | `list_by_field("user_id")` in linking and user administration |
| `account_session_token_uq` | `(token_digest)` where `account_sessions` | session authentication (`find_by_field("token_digest")`) |
| `account_session_account_idx` | `(account_id)` | no `list_by_field("account_id")` on sessions was found in the code |
| `account_session_expiry_idx`, `account_session_revoked_idx` | `(expires_at, record_id)` / `(revoked_at, record_id)` | `list_before` in the bounded cleanup (`account_cleanup.py`) |
| `account_oauth_state_uq` | `(state_digest)` | callback lookup `find_by_field("state_digest")` |
| `account_oauth_state_expiry_idx` | `(expires_at, record_id)` | bounded cleanup |
| `account_org_request_key_uq` | `(account_id, key_digest)` where `account_organization_requests` | idempotency; its `account_id` prefix serves the quota count `list_by_field("account_id")` |
| `user_email_lower_idx` | `(lower(payload->>'email'))` where `users` | `list_by_field("email", casefold=True)` across all organizations (`account_linking.py`) |
| `organization_lifecycle_state_idx` | `(state, record_id)` where `organization_lifecycle` | `list_by_field("state")` in the purge tick |
| `product_installations_plugin_owner_idx` (after migration 20) | `(payload->>'plugin_id', org_id, record_id)` where `plugin_installations` | trigger and catalog existence checks on `plugin_id` (`has_installations`, delete guards, ownership report) |
| `product_login_throttle_window_idx` (`product_login_throttle`) | `(window_started_at)` | `DELETE ... WHERE window_started_at < cutoff` in `_maybe_prune` |
| `product_secrets_org_idx` (`product_secrets`) | `(org_id)` | `purge_organization` (`WHERE org_id = ...`) |
| `product_plugin_owner_org_idx` (`product_plugin_ownership`) | `(org_id, plugin_id)` where state owned | owner lookups |

## 3. Triggers and functions

| Object | Definition | Purpose |
|---|---|---|
| Function `weaves_system_allows(coll text)` | SQL, `STABLE`; `CASE app.system_purpose` returns whether the named purpose lists the collection | RLS; re-created at each start-up (CREATE OR REPLACE), see 5 |
| Function `weaves_rls_allows(coll, rid, row_org)` | SQL, `STABLE` | RLS policy predicate |
| `guard_plugin_record()` and trigger `plugin_record_guard` | `BEFORE INSERT OR UPDATE OR DELETE ON product_records` (migration 20) | descriptor identity immutable, no workspace on a descriptor, `payload.plugin_id = record_id`, owner and `plugin_type` immutable, deletion refused for built-in or quarantined or installed descriptors (SQLSTATE 23514), owner organization must exist |
| `register_plugin_owner()` and trigger `plugin_record_owner` | `AFTER INSERT ON product_records` | inserts the ownership row (`builtin` for `github`/`slack`/`jira` with `org_id NULL`, `owned` when an organization is set, otherwise `quarantined` with reason `unowned_or_reserved_descriptor`); rejects reserved ids with an owner |
| `guard_plugin_installation_owner()` and trigger `plugin_installation_owner_guard` | `BEFORE INSERT OR UPDATE ON product_records` | installation identity, org, workspace and `plugin_id` immutable; payload scope equals columns; workspace exists in the organization; descriptor not quarantined and, if owned, owned by the same organization |
| `guard_plugin_ownership()` and trigger `plugin_ownership_guard` | `BEFORE UPDATE OR DELETE ON product_plugin_ownership` | ownership rows immutable and not deletable independently |
| `guard_plugin_rollout()` and trigger `plugin_rollout_guard` | `BEFORE UPDATE OR DELETE ON product_plugin_ownership_rollout` | rollout evidence immutable |

The five migration-20 functions run with `search_path = pg_catalog, <schema>`
(`_install_guards`).

## 4. Roles, grants

* Role `weaves_app`: `NOLOGIN NOSUPERUSER NOBYPASSRLS`, created by migration 21 when
  the migrating role may create roles (otherwise a notice is raised and policies stay
  unused). A login role must be granted membership to use it; `WEAVES_DB_APP_ROLE`
  names the role the store assumes with `SET LOCAL ROLE` semantics
  (`set_config('role', ..., true)`).
* `APP_ROLE_GRANTS` (re-run by migration 21, by `activate_ownership` and by the secret
  store): `GRANT USAGE ON SCHEMA`, and `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL
  TABLES IN SCHEMA` to `weaves_app`. In the snapshot `weaves_app` holds DML on
  `product_records`, `product_secrets`, `product_login_throttle`,
  `product_schema_migrations`, `product_plugin_ownership` and
  `product_plugin_ownership_rollout`. No `ALTER DEFAULT PRIVILEGES`: a new table gets
  grants only when a later step re-runs the grant.
* Table owner: the migrating connection. Owners are exempt from row-level security
  because `FORCE ROW LEVEL SECURITY` is not set (`pg_class.relforcerowsecurity = false`
  for all tables).

## 5. Row-level security

Source: `infrastructure/database/row_security.py` (policy functions re-applied at each
start-up by `REPEATABLE_STATEMENTS`; the policy itself created by migration 21).

Snapshot: row security enabled on `product_records` and `product_secrets` only (not on
`product_login_throttle`, `product_schema_migrations`, `product_plugin_ownership*`).

| Table | Policy | Applies to | Using / with check |
|---|---|---|---|
| `product_records` | `tenant_isolation` (permissive, `FOR ALL`, role `public`) | all commands | `weaves_rls_allows(collection, record_id, org_id)` for both `USING` and `WITH CHECK` |
| `product_secrets` | `tenant_isolation` (permissive, `FOR ALL`, role `public`) | all commands | `org_id = NULLIF(current_setting('app.org_id', true), '')` for both |

`weaves_rls_allows(coll, rid, row_org)` evaluates, in order:
1. `coll = 'organizations'`: allow if `rid = app.org_id` or the system purpose lists `organizations`.
2. `coll IN ('accounts', 'account_identities', 'account_oauth_states', 'account_sessions',
   'account_organization_requests')`: allow only if the system purpose lists the collection.
3. `row_org IS NULL`: allow (every tenant and every purpose).
4. Otherwise allow if `row_org = app.org_id` or the system purpose lists the collection.

`weaves_system_allows(coll)` reads `app.system_purpose` (set per transaction from
`system_scope(...)`): `bootstrap` allows every collection; the other purposes (`auth`,
`queue`, `dispatch`, `recovery`, `studio`, `account`, `lifecycle`, `catalog`, `oauth`)
allow the exact lists in [generated.md](generated.md) section 4; any other value
(including the `operator` scope used by `infra/gcp/singlevm/backfill_schedule_timezones.py`)
allows nothing, so operator scripts depend on connecting as the table owner.
`memberships` is not in the account branch: it is tenant-matched or visible under `auth`
and `account`. `product_secrets` has no system-purpose branch at all, so purge and
secret reads need the organization's own scope.

Interaction with the code: unscoped repository calls (`get`, `list`, `find_by_field`,
`delete`) have no `org_id` predicate in SQL; with `WEAVES_DB_APP_ROLE` set they are
confined by this policy, without it they are global.

## 6. Migration history

Runner: `ProductPostgresStore._migrate` (`infrastructure/database/store.py`). It creates
`product_schema_migrations`, takes `pg_advisory_xact_lock(hashtextextended(
'weaves-product-schema-migrations', 0))`, rejects a database containing a version it
does not know (`_READ_COMPATIBLE_MIGRATIONS = {19, 20, 21}` extends the known set for
reading), validates the ownership schema if version 20 is applied, applies the
missing versions in order (all statements, then the version row, in one transaction),
then re-runs `REPEATABLE_STATEMENTS` (policy functions, `ACCOUNT_SCHEMA` indexes,
`LIFECYCLE_SCHEMA` index). Everything is in `store.py` unless noted. Versions 1 to 21 are
marked **history**: they are applied once and are not edited.

| Version | Status | What it does | Still relevant |
|---|---|---|---|
| 1 | history | Creates `product_records` (and `ADD COLUMN IF NOT EXISTS` for the two idempotency columns on older databases), backfills the run idempotency columns, scope indexes, uniqueness for run and job idempotency, API token digest, role-binding scope, and the job claim index | yes, the base table |
| 2 to 5 | history | No DDL. Recorded by the original bootstrap and kept reserved so existing databases upgrade without rewriting history | no |
| 6 | history | Unique index on `user_sessions` token digest | no: legacy password sign-in removed (commit `4e90010`); no repository |
| 7 | history | Unique index on `user_invitations` digest | no: dropped by version 15 |
| 8 | history | Unique active user email per organization | yes |
| 9 | history | Creates `product_login_throttle` and its window index | yes |
| 10 | history | Unique webhook secret digest | yes |
| 11 | history | Index for due active schedules | yes |
| 12 | history | Backfills `created_at` of `audit_events` from the payload and adds the two audit paging indexes | yes |
| 13 | history | Unique system role key per organization | yes |
| 14 | history | Task and task-turn idempotency uniqueness, task and message paging indexes, turn claim and stale-lease indexes | yes |
| 15 | history | Drops the `user_invitations` index and deletes those rows | yes (data removal) |
| 16 | history | Data migration after Knowledge/Memory removal: strips `memory_policy` and the `knowledge.search` capability from agent and workflow versions and roles, deletes the `local-knowledge` plugin and its installations | no, data only |
| 17 | history | Skill uniqueness (organization slug, personal slug, version number, publication per version) | yes |
| 18 | history | One revocation per skill version | yes |
| 19 | history | SSO identity unique indexes, `sso_connections` index, `reject_sso_identity_rewrite` function and `sso_identity_immutable` trigger | questionable: no repository for these collections (see observations) |
| 20 | history, operator-activated | Catalog ownership (`plugin_ownership.py`, `plugin_ownership_sql.py`): two tables, guards, backfill of `org_id` onto descriptors, rollout evidence. Never applied by start-up; absent from a fresh database; requires versions 1 to 19 and a recorded reader image digest, takes `ACCESS EXCLUSIVE` on `product_records`, bounded to 10,000 records | yes once activated |
| 21 | history | Row-level security (`row_security.py`): `weaves_app` role, policy functions, `ENABLE ROW LEVEL SECURITY`, `tenant_isolation` policy, grants | yes |
| 22 | start-up | `legacy_purge.py`: deletes rows of the purged collections (`user_passwords`, `user_password_credentials`, `user_sessions`, `user_invitations`, `sso_connections`, `sso_external_identities`, `email_login_challenges`, `account_login_challenges`, `memory_items`, `knowledge_documents`); drops `product_records_user_session_digest_uq`, `product_records_user_invitation_digest_uq`, `sso_identity_subject_uq`, `sso_identity_user_connection_uq`, `sso_connections_org_provider_idx`, `account_challenge_email_idx`, `account_challenge_expiry_idx`, trigger `sso_identity_immutable` and function `reject_sso_identity_rewrite()` | yes (idempotent) |
| 23 | start-up | `organization_identity.py`: backfills `organization_settings.slug` and `.email_domain` (idempotent, only rows still lacking a value); creates `organization_slug_uq` and `organization_email_domain_uq`, partial unique expression indexes on `lower(payload->>'slug')` and `lower(payload->>'email_domain')` for `organization_settings` | yes (idempotent) |
| repeatable | every start | Policy functions (`CREATE OR REPLACE`), account-layer indexes (`account_schema.py`), `organization_lifecycle_state_idx` | yes, idempotent |
| out of band | secret store start | `product_secrets` create/alter/policy/backfill | yes |

### Version 23

Migration 23 is in the table above; see `docs/DATA_MODEL.md` (organization slug and email domain).

### Version 22

Migration 22 is in the table above.


Migrations are append-only and forward-only; the reader-compatibility set
(`_READ_COMPATIBLE_MIGRATIONS`) no longer exists. Migrations 6, 15 and 19 stay as
history and create objects that 22 removes. A bounded organization-purge needs no
migration. Later migrations are recorded here.

## 7. Observations for the review

Listed, not fixed. "Verified" means the statement was read directly from code or the
introspected database; "unverified" means plan or volume behaviour not measured.

Stale or unused objects
1. (Resolved by migration 22) the legacy session and SSO indexes and the SSO trigger.
2. (Resolved by migration 22) legacy collection names no longer appear in `PURGE_ORDER`.
3. `account_identity_account_idx` duplicates the leading column of `account_identity_owner_uq`.
   Verified from definitions.
4. `account_session_account_idx`: no query by sessions' `account_id` was found
   (`list_by_field`/`find_by_field` on sessions use `token_digest`, `expires_at`, `revoked_at`).
   Verified by grep; may exist for future "list my sessions".
5. `product_records_workflow_trigger_secret_uq` and `product_records_role_binding_scope_uq` serve
   uniqueness only (acceptable).
6. (Resolved by migration 22) no collection without a repository remains.
7. `product_secrets` rows with `org_id IS NULL` are unreadable (policy) and are never deleted
   by purge (`purge_organization` filters by organization). Orphans are possible. Verified from
   code, not observed.
8. `REPOSITORY_SPECS` creates a generic repository for `plugins` and then replaces it with the
   catalog adapter in `create_product_repository_set`.

Integrity and security
9. No foreign keys except `product_plugin_ownership`; see [../DATA_MODEL.md](../DATA_MODEL.md) 5.1.
10. `weaves_app` has `UPDATE`/`DELETE` on `product_schema_migrations` and on
    `product_plugin_ownership*` and on every table (the plugin tables are protected only by
    triggers). Verified in the snapshot.
11. `FORCE ROW LEVEL SECURITY` is off; policy only protects connections that assume `weaves_app`
    (`WEAVES_DB_APP_ROLE`). The policy names role `public` rather than `weaves_app`.
12. `weaves_app` is created `NOLOGIN`; the provisioning that makes a login role a member is not in
    this repository's migrations (`infra/` was not reviewed here).
13. Policy `org_id IS NULL` pass-through (3 in section 5) and `bootstrap` purpose allowing every
    collection. Verified.
14. `product_login_throttle`, `product_schema_migrations`, `product_plugin_ownership*` have no RLS.
15. The literal `"lifecycle"` is used as the workspace id of the request identity in operator
    and purge flows (`organization_lifecycle.py`, `organization_purge.py`, and the
    deletion-recovery route in `runtime_organizations.py`), so a workspace-id string that is not
    a real workspace appears in request context (and possibly in `workspace_id` columns written
    in that context; not verified for each collection).

Performance and design
16. Several recovery and studio paths use `.list()` over a whole collection (`workflow_runs`,
    `execution_jobs`, `tool_invocations`, `canvas_settings`, `canvas_runs`); see
    [../DATA_MODEL.md](../DATA_MODEL.md) section 7. Unverified at volume.
17. `list_status_updated_before` is generic (`payload->>'status' = $1 AND updated_at <= $2 ORDER BY
    updated_at`), but the supporting partial index exists only for `task_turns`. Unverified.
18. `find_by_field` returns `LIMIT 1` without `ORDER BY` (`postgres_query_repository.py`); used with
    non-unique fields such as identity `subject` and principal `user_id`.
19. `principals.find_by_field("user_id")` has no supporting index. Unverified at volume.
20. Index keys on JSON timestamps are text; lexical order equals time order only with uniform UTC
    `Z` formatting.
21. `created_at` column equals the payload value only for rows written through `create()`; a row first
    written through `put()` takes the database clock, and paging indexes sort on that column.
22. `updated_at` is written by two clocks (database `now()` for `put`, application value for `put_if_*`).
23. The RLS policy calls a non-`LEAKPROOF` SQL function on every row; whether the planner still uses the
    partial indexes under the `weaves_app` role was not measured (an `EXPLAIN` under that role at
    realistic volume is a suggested review step).
24. Export (`organization_export.py`) builds the whole document in memory with a 50 MB default limit; it
    is a bounded copy, not a streaming backup.
25. Migration 20 activation takes an `ACCESS EXCLUSIVE` lock on `product_records` and is limited to 10,000
    records; a larger database needs a separate rollout.
