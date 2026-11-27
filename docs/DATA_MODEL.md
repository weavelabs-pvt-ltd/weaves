# Weaves backend data model

Status: derived from the code at `origin/main` (commit `b2b2751`) and from a real
migrated PostgreSQL database, not from earlier prose. Every claim names the file
it comes from. This document is the entry point for the ER review, the database
schema review, and the engineer who builds quotas, recovery, billing and
enterprise identity (phases 5 to 8).

| Document | Contents |
|---|---|
| This file | Storage architecture, collection catalogue by domain, invariants and gaps, data flows, volume notes, review questions |
| [data-model/er-diagrams.md](data-model/er-diagrams.md) | Mermaid ER diagrams: one per domain plus an overview |
| [data-model/physical-schema.md](data-model/physical-schema.md) | Introspected PostgreSQL schema, row-level security, grants, migration history, observations for the review |
| [data-model/generated.md](data-model/generated.md) | Generated from code: every collection's fields, the index inventory, the system scopes. Kept current by `scripts/export_data_model.py` |

Conventions. A *collection* is a logical entity type (the value of
`product_records.collection`). A *record* is one row of `product_records`.
"Reference" means a field that holds another record's id. No reference is a
database foreign key (section 1.6).

## 1. Storage architecture

### 1.1 What is stored where

| Table | Purpose | Created by |
|---|---|---|
| `product_records` | Every product entity: 48 collections (section 2), one row per record, body in `payload jsonb` | migration 1, `infrastructure/database/store.py` |
| `product_secrets` | Fernet ciphertext of provider keys and connector tokens, owned by one organization | `EncryptedPostgresSecretStore.__init__`, `infrastructure/secrets/store.py` (at start-up, only when `WEAVES_SECRET_ENCRYPTION_KEY` is configured, not by a numbered migration) |
| `product_login_throttle` | Fixed-window attempt counters for sign-in and organization creation | migration 9 |
| `tenant_scheduler_state` | Last claim time per organization, for round-robin worker claiming; no tenant content, pruned after a day, row security admits only the `queue` system purpose | migration 26 |
| `usage_ledger`, `usage_daily` | Per-organization model-call usage (tokens, integer nano-USD) and its daily rollup; a deliberate exception to the one-table model | migration 27, `infrastructure/database/usage_schema.py` |
| `slack_installations`, `slack_event_inbox` | The Slack bot: one workspace bound to one organization (token only as a secret-store reference) and the global, deduplicated queue of signed Slack events; a deliberate exception to the one-table model | migration 32, `infrastructure/database/slack_schema.py` |
| `platform_controls`, `operator_actions`, `org_quarantines` | Runtime kill switches (one row, NULL = environment default), the append-only operator audit trail and what each tenant quarantine changed; no tenant content; the restricted role can only read the switches | migration 30, `infrastructure/database/controls_schema.py` |
| `product_schema_migrations` | Applied migration versions | `ProductPostgresStore._migrate` |
| `product_plugin_ownership`, `product_plugin_ownership_rollout` | Explicit ownership of plugin descriptors, plus immutable rollout evidence | migration 20, applied only by an operator (`infrastructure/database/plugin_ownership.py`), never by start-up |

`product_records` columns: `collection text`, `record_id text` (primary key
together), `org_id text null`, `workspace_id text null`, `idempotency_key text
null`, `requested_by_principal_id text null`, `payload jsonb not null`,
`created_at timestamptz default now()`, `updated_at timestamptz default now()`.
The full physical definition is in [data-model/physical-schema.md](data-model/physical-schema.md).

### 1.2 Why a single JSONB table (history)

The backend was introduced on 2026-10-06 (commit `6c126ef`, "Replace website with
Weaves platform backend") with `product_records` already in place. The product
was defined contract-first: strict, versioned pydantic models in
`contracts/v1/` (`ProductContract` forbids undeclared fields, is strict, and
carries `schema_version`). `docs/product-runtime-persistence.md` states the
design: "`PostgresRepository` stores versioned contract JSON in `product_records`
and validates every read through the contract's strict JSON parser". The aim was
to let contracts evolve quickly without a DDL change per field, and to keep an
identical in-memory implementation for local development and tests. The columns
that are not JSON (`org_id`, `workspace_id`, `idempotency_key`,
`requested_by_principal_id`, `created_at`, `updated_at`) were promoted over time
for tenant scoping, idempotency and keyset paging. The schema of each entity is
therefore owned by the Python contract; the database enforces almost none of it.

### 1.3 The repository abstraction

* Port: `application/ports/repository.py`, `Repository[TContract]` (a `Protocol`).
  Beyond create/put/get/delete it includes purpose-built operations rather than a
  generic query language: `get_scoped`, `list_scoped`, `find_by_idempotency_key`,
  `list_due`, `list_audit_page`, `list_task_page`, `list_task_messages_page`,
  `list_status_updated_before`, `list_before`, `claim_next_queued` (a
  `FOR UPDATE SKIP LOCKED` queue claim), and compare-and-set updates
  (`put_if_status_and_stale`, `put_if_worker_owned`, `put_if_task_state`).
  `find_by_field` and `list_by_field` accept any contract field name (validated as
  a plain identifier) and compile to `payload->>'field' = value`.
* Registry: `ProductRepositorySet` (`application/ports/product_repositories.py`)
  is a name-to-repository map. `REPOSITORY_SPECS` in
  `infrastructure/persistence/product_repositories.py` is the single list of
  `(name, collection, id_field, contract class)` tuples. Its collection list is
  the authoritative catalogue of this document and is checked by
  `tests/tenancy/test_ownership_contract.py` against `tenancy_harness/ownership.json`.
* Adapters: `PostgresRepository` (`postgres_repository.py`, with queries in
  `postgres_query_repository.py`) and `InMemoryRepository` (`memory_repository.py`)
  implement the same protocol. `plugins` is the exception: it uses
  `PostgresPluginCatalog` / `InMemoryPluginCatalog`, which add ownership-aware
  visibility on top of the same table. Bulk, organization-wide operations
  (export paging, purge batches, work cancellation) go through a separate port,
  `OrganizationData` (`application/ports/organization_data.py`).
* Every read goes through `model_validate_json`, so stored JSON that no longer
  satisfies the current contract fails at read time, not at write time in the
  database.
* Tenant-keyed collections: `TENANT_KEYED_COLLECTIONS = {evaluation_suites,
  evaluation_reports}`. Their ids are chosen by clients (suite names), so the
  stored `record_id` is `"{org_id}:{id}"` and `get_scoped` looks up both that and
  the bare id. All other collections store the bare id as `record_id`, so ids
  must be globally unique across tenants (they are generated UUIDs, except where
  noted in section 2).

### 1.4 Unit of work and per-connection scoping

* `ProductPostgresStore.transaction()` (`store.py`) borrows one pooled connection,
  stores it in a `ContextVar`, and every repository call inside the `with` block
  reuses it, so a group of repository calls commits or rolls back together
  (psycopg commits when the block exits normally). Nested calls join the outer
  transaction. Services receive this as `unit_of_work`
  (`apps/runtime_wiring.py::_build_unit_of_work`); the in-memory equivalent
  `InMemoryUnitOfWork` locks and snapshots all in-memory repositories.
* Tenant scope per connection: `store.connection()` calls `_enter_tenant_scope`
  every time a repository borrows or re-uses a connection. When `WEAVES_DB_APP_ROLE`
  names a role, it runs `set_config('role', <role>, true)`, `set_config('app.org_id',
  <org>, true)` and `set_config('app.system_purpose', <purpose>, true)` with
  `is_local = true`, so the settings live only for the current transaction and a
  pooled connection cannot carry one tenant into the next. The org comes from
  `shared/request_context.py::storage_org_scope()`: the authenticated request
  identity, else empty in strict (production) processes. The purpose comes from
  `system_scope("...")` blocks. Row-level security policies then compare
  `current_setting('app.org_id')` and `app.system_purpose` with each row
  (section 5 of [physical-schema.md](data-model/physical-schema.md)).
* If `WEAVES_DB_APP_ROLE` is not set, no role is assumed and the policies are not
  evaluated for the connecting (owner) role. The tenant predicate is then enforced
  only by the `org_id = ...` clauses written into the repository SQL.
* Cross-record invariants are serialised with advisory transaction locks
  (`acquire_advisory_transaction_lock`, `pg_advisory_xact_lock(hashtextextended(name,
  0))`). Names in use: `weaves-product-schema-migrations` (migrations and catalog
  activation), `weaves-org-administration:{org_id}` and `account-organization:{account_id}`.

### 1.5 The per-organization administration lock

`OrganizationGuard.mutation()` (`modules/auth/organization_guard.py`) opens a unit
of work and takes `pg_advisory_xact_lock("weaves-org-administration:" + org_id)`.
It is released at commit or rollback. The decorator `serialized_administration`
(`modules/auth/administration_transaction.py`) wraps every method that changes
ownership or effective administration. The same lock name is taken by
`MembershipLinker._create` (first-sign-in membership linking) and by the lifecycle
service and the purge finaliser, so these serialise per organization. It protects
the invariants "an organization keeps at least one active human owner/admin" and
"no membership is linked into an organization that is leaving ACTIVE" (both
implemented in code, not in the database; section 5).

### 1.6 Trade-offs and questions an architect should examine

Stated candidly; see section 8 for the checklist.

1. Single JSONB table versus typed tables. Gains: schema evolution without DDL;
   one purge, export and RLS mechanism for all entities; trivial parity with the
   in-memory adapter. Costs: no column types, `NOT NULL`, `CHECK` or enum
   constraints on the business data; every uniqueness and lookup rule is a
   partial expression index on `payload->>'x'` (text comparison, including for
   timestamps and numbers); `SELECT payload` always materialises the whole
   document; a table-wide `VACUUM`/bloat/statistics profile mixes tiny rows
   (`memberships`) with large ones (`audit_events`, `task_messages`, `workflow_versions`);
   per-collection statistics are impossible, so planner estimates for partial
   indexes depend on predicate matching only.
2. FK-less references. Every reference field (section 2) is application-checked. There
   is no referential integrity, no `ON DELETE` behaviour, and deletion order is a
   hand-maintained list (`PURGE_ORDER`). Orphans are possible after any partial
   failure. The only database-level foreign key in the system is
   `product_plugin_ownership.(record_collection, plugin_id)` to `product_records`.
3. Uniqueness only through partial expression indexes. Each index is hard-coded to
   one `collection` value and a specific JSON path. A new collection or a renamed
   field silently has no uniqueness until someone writes the index. Several indexes
   cover collections that no longer have a repository (section 2.9 and the
   observations).
4. Global versus tenant keys. `PRIMARY KEY (collection, record_id)` is global:
   two organizations cannot use the same id. This is safe for generated ids but is
   why client-named collections needed the `org:id` key trick, and why
   `account_organization_requests` ids are SHA-256 digests. Several unique indexes
   are global by design (account identity subject, session digest, API token digest,
   webhook secret digest) and several are per organization (user email, skill slug).
5. `org_id` is a nullable column. A row with `org_id IS NULL` passes the RLS policy
   for every tenant (`WHEN row_org IS NULL THEN true` in `weaves_rls_allows`) except
   for the collections the policy lists as account-scoped and the two waitlist
   collections (migration 24), which only the `waitlist` system purpose can see. Which collections may be
   unowned is a code convention (section 5).
6. Two clocks. `product_records.updated_at` is `now()` on a plain `put`, but the
   compare-and-set updates write the application's `updated_at` from the payload.
   Stale-lease recovery (`list_status_updated_before`) uses the column.
7. Retention and size. See section 7.

## 2. Collection catalogue

Field-level detail (types, constraints, enum values) for every collection is in
[data-model/generated.md](data-model/generated.md), generated from the contracts.
The tables below give what the code cannot self-document: scope rationale,
lifetime, deletion, access and export.

Legend.
*Scope* is the value of the `org_id` / `workspace_id` columns for the row.
`global` rows have no `org_id`; `account` rows are keyed by account, not by
organization; `organization` rows have `org_id`; `workspace` rows have both.
*Mutability*: `mutable` (has `updated_at`, updated in place), `immutable` (frozen
contract, only created or deleted), `replace` (no timestamps, whole record
replaced).
*Purge* is the position in `PURGE_ORDER` of `modules/auth/organization_retention.py`
(deleted in that order in bounded batches of 200) or `kept` (tombstone/ledger
retained after organization deletion). *Export* is membership of `EXPORT_COLLECTIONS`
in `modules/auth/organization_export_rules.py`.
Permissions are `contracts/v1/identity.py::Permission` names checked in the
service layer (`modules/*`); "owner" means `OrganizationSettings.owner_principal_id`.
Permission names are quoted from the call sites found by `grep Permission.`; where a
service delegates to a helper, the permission of the helper is given.
The "JSON paths in indexes" column lists the `payload->>'x'` expressions that the
indexes in [generated.md](data-model/generated.md) use for the collection.

### 2.1 Identity and accounts

An *account* is a platform-wide person. An *organization member* (`users` row plus a
`principals` row) is tenant-local. A `memberships` row links the two and is the only
bridge between the global and the tenant layers
(`contracts/v1/accounts.py`, `modules/auth/account_selection.py`).

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `accounts` (`Account`, `id`) | account. A person exists once across all organizations | mutable. `status`: active, disabled (`AccountStatus`); `display_name` 1..160; optional `role_title` 1..80 trimmed, no control characters (None clears; global profile text, never copied into a tenant) | none outward. Referenced by `account_identities.account_id`, `account_sessions.account_id`, `memberships.account_id`, `account_organization_requests.account_id` (account owns them) | kept; no deletion flow exists in code. Not exported | Sign-in and `/account` routes only, under system scope `account`/`auth`; no tenant permission | none |
| `account_identities` (`AccountIdentity`, `id`) | account. A verified login identity belongs to one account platform-wide | mutable. `kind` (`AccountIdentityKind`), `issuer`, `subject` 3..320, `verified_at` | `account_id` to `accounts` (account owns) | kept; removed only by identity removal (`account_identity_removal.py`). Not exported | Account routes under scope `account` | `kind`, `issuer`, `subject`, `verified_at`, `account_id` |
| `account_sessions` (`AccountSession`, `id`) | account. Sessions are created before an organization is chosen | mutable. `token_digest` (sha-256 hex, 64), `channel` bearer/browser, `expires_at` (12 h, `ACCOUNT_SESSION_HOURS`), `revoked_at` | `account_id` to `accounts` | kept by purge; bounded cleanup of rows 1 h past expiry or revocation (`account_cleanup.py`, worker every 30 s). Not exported | scope `account` | `token_digest`, `account_id`, `expires_at`, `revoked_at` |
| `account_oauth_states` (`AccountOAuthState`, `id`) | account. Google sign-in attempt, single use | mutable. `state_digest`, `binding_digest`, `nonce`, `code_verifier`, `expires_at` (10 min) | none | kept; cleaned up after expiry, consumed on callback (`consume`). Not exported | scope `account` | `state_digest`, `expires_at` |
| `account_organization_requests` (`AccountOrganizationRequest`, `id` = sha-256 of account id and key digest) | account. Idempotency record and per-account organization-creation quota ledger | immutable (`created_at` only). `key_digest`, `request_fingerprint`, `organization_id` | `account_id`, `organization_id` | kept forever on purpose (a deleted organization still counts toward the quota). Not exported | scope `account` | `account_id`, `key_digest` |
| `memberships` (`OrganizationMembership`, `id`) | organization. It is the account-to-tenant link, so it carries both `account_id` and the tenant's `org_id` | mutable. `status` (`MembershipStatus`), `account_id`, `user_id`, `principal_id` | `account_id` to `accounts`; `user_id` to `users`; `principal_id` to `principals`. Organization owns it | purge position 36 (and a final sweep in `_finalize_when_clean`). Exported with fields id, user_id, principal_id, status, timestamps only (account ids withheld) | read and write `roles.manage` (`runtime_memberships.py`), `users.manage` for linking; sign-in code under scope `account` | `account_id`, `user_id` |

| `waitlist_entries` (`WaitlistEntry`, `id` = `wle_` + sha-256 of the lower-cased email) | global (no `org_id`). One row per email; the derived id makes the primary key the race-safe uniqueness rule and the unique index `waitlist_entry_email_uq` on `lower(email)` the backstop | mutable. `email` lower-cased, 3..254; `email_domain`; `domain_type` company/public (`shared/organization_naming.py` plus `WEAVES_PUBLIC_EMAIL_DOMAINS`); `status` new/reviewed/contacted/converted; `notes` <=2000 (operator only); `first_joined_at`, `last_joined_at`, `submission_count`; `history` (<=50 operator actions: action, actor_kind `operator`, operator, time) | none outward. `waitlist_submissions.entry_id` points at it | bounded retention purge (an entry left with no submissions is deleted) and operator `delete`. Never exported and never touched by an organization purge (`KEEP_COLLECTIONS`) | system scope `waitlist` only (public waitlist routes, worker retention tick); operator script on the owner connection; every tenant and every other system purpose is refused by row policy | `email` (lower, unique) |
| `waitlist_submissions` (`WaitlistSubmission`, `id`) | global (no `org_id`). One row per join from one browser | mutable. `token_hash` (sha-256 of the private `wlt_` resume token; the token is never stored), `token_expires_at` (30 days), `question_version`, `answers` by question id (`value` option id / ids / band / text, optional `other` <=60; required on `other` for every `allow_other` question of the submission's set, so set 2 asks it on role, company_location, waiting_on and tools while answers stored under set 1 may hold a bare `other`), `consent_version`, `consented_at`, `source` (page, referrer, utm_*), optional `idempotency_digest` | `entry_id` to `waitlist_entries` | deleted when not updated for `WEAVES_WAITLIST_RETENTION_DAYS` (default 365, worker tick hourly) and by operator `delete`. Never exported or purged with an organization | as `waitlist_entries` | `token_hash` (unique), `entry_id`, `created_at` |

### 2.2 Tenancy, access control and lifecycle

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `organizations` (`Organization`, `id`) | global row whose `record_id` is the organization id; RLS matches `rid = app.org_id` | mutable. `name` 1..160, `status` active/suspended/deleted. Status is a projection of lifecycle (`lifecycle_state.py`) | root of the tenant tree; everything else points to it by `org_id` | kept as a tombstone (name replaced by "deleted organization"). Not exported as a collection but included as `organization` in the document | `organizations.admin`; lifecycle code under scope `lifecycle` | none |
| `organization_settings` (`OrganizationSettings`, `id` = org id) | organization. Owner, timezone, locale, slug, claimed email domain; one row per organization | mutable, optimistic `revision`. `owner_principal_id`, `timezone` default `Asia/Kolkata`, `locale`, `slug` (3..40 of `[a-z0-9-]`, globally unique without case among non-deleted organizations, identifier only, no routing), `email_domain` (lowercase custom domain claimed at creation; unique; read-only) | `owner_principal_id` to `principals` | purge position 40. Included in export as `settings` | `organizations.admin`; owner transfer restricted to the current human owner | none |
| `organization_lifecycle` (`OrganizationLifecycle`, `id` = org id) | organization. State machine record, separate from `Organization.status` so older readers stay valid | mutable. `state` active/suspended/deletion_requested/deleting/deleted; `grace_deadline`, `purge_now`, `purge_step`, `purged_rows`, `clean_ticks` 0..100, `revision`, `history` (max 200 entries) | `id` equals `organizations.id` | kept (accountability record). Exported inside the document as `lifecycle` | read: owner and organization admins; write: owner (request/cancel deletion) and operator scripts. Worker discovery under scope `lifecycle` | `state` |
| `workspaces` (`Workspace`, `id`) | organization. Workspaces partition work inside an organization; the `workspace_id` column of workspace-scoped rows refers here | mutable. `name` 1..160, `environment`, `status` active/archived | `org_id` to `organizations` (organization owns) | purge position 39; exported | write `workspaces.admin`; read via role bindings (`list_principal_workspaces`) | none |
| `users` (`User`, `id`) | organization. A tenant-local member profile | mutable. `email` 3..320, `display_name`, `status` active/invited/suspended/deleted | `org_id` | purge position 38; exported (id, display_name, email, status, timestamps) | `users.manage` | `email` (lowercased), `status` |
| `principals` (`Principal`, `id`) | organization. The authorization subject; user, service account, system or external app | mutable. `principal_type` user/service_account/system/external_app, `status` active/disabled, `user_id` (required iff type is user, checked by a model validator) | `user_id` to `users` | purge position 37; not exported | `users.manage` and `roles.manage` | none |
| `api_credentials` (`ApiCredential`, `id`) | organization. Service tokens, authenticated before the tenant is known (system scope `auth`) | mutable. `token_digest` (sha-256 hex), `status` active/revoked, `expires_at`, `revoked_at` | `principal_id` to `principals` | revoked first and then deleted at position 33 by purge; never exported | `users.manage`, `roles.manage` (`runtime_credentials.py`) | `token_digest` |
| `roles` (`Role`, `id`) | organization | replace (no timestamps). `permissions` list of `Permission` (unique, validated), `system_role_key` admin/builder/user | `org_id` | purge position 35; exported | `roles.manage` | `system_role_key` |
| `role_bindings` (`RoleBinding`, `id`) | organization (workspace optional). the `workspace_id` column mirrors the payload field and is `NULL` for organization-wide bindings | replace (only `created_at`) | `principal_id` to `principals`, `role_id` to `roles`, optional `workspace_id` to `workspaces` | purge position 34; exported | `roles.manage` | `principal_id`, `role_id`, `workspace_id` |

### 2.3 Agents, workflows, tasks and runs

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `agents` (`AgentDefinition`, `id`) | workspace | mutable. `status` draft/active/archived | `current_version_id` to `agent_versions` (agent owns versions) | position 20; exported | read `agents.view`, write `agents.manage` | none |
| `agent_versions` (`AgentVersion`, `agent_version_id`) | workspace. Frozen snapshot used by runs | immutable | `agent_id`, `model_profile_id`, `allowed_workflow_ids`, `allowed_capability_ids`, `budget_policy_id` | position 19; exported | `agents.manage` / `agents.view` | none |
| `workflows` (`WorkflowDefinition`, `workflow_id`) | workspace | mutable. `status` draft/active/archived | `current_version_id` to `workflow_versions` | position 18; exported | `workflows.view` / `workflows.manage` | none |
| `workflow_versions` (`WorkflowVersion`, `workflow_version_id`) | workspace | immutable | `workflow_id`, `agent_version_ids`, `required_capability_ids` | position 17; exported | `workflows.manage` | none |
| `workflow_triggers` (`WorkflowTrigger`, `trigger_id`) | workspace. Webhook triggers | mutable. `trigger_type` webhook, `status` active/disabled, `secret_digest` | `workflow_id`, `created_by_principal_id` | position 16; exported (digest scrubbed) | `workflows.manage`, `workflows.run`, `agents.run` | `secret_digest` |
| `workflow_schedules` (`WorkflowSchedule`, `schedule_id`) | workspace. Calendar occurrences | mutable. `status` active/disabled/error, `next_run_at`, timezone | `workflow_id`, `created_by_principal_id` | position 15; exported | `workflows.manage` | `status`, `next_run_at` |
| `workflow_runs` (`WorkflowRun`, `run_id`) | workspace. One execution of a workflow version | mutable. `status` queued/running/succeeded/failed/cancelled; `idempotency_key` (also a column) | `workflow_id`, `workflow_version_id`, `requested_by_principal_id`, `trigger_ref` | position 10; exported. Grows unbounded | `workflows.run`, `agents.run`; reads `artifacts.read` | idempotency columns (not JSON) |
| `agent_runs` (`AgentRun`, `agent_run_id`) | workspace. One agent execution, child of a workflow run | mutable. `status` as `RunStatus`; `resolved_provider_id`, `resolved_model_id`, usage and estimated cost | `run_id` to `workflow_runs`, `agent_id`, `agent_version_id`, `model_profile_id` | position 9; exported. Grows unbounded | `agents.run` | none |
| `tool_invocations` (`ToolInvocation`, `invocation_id`) | workspace. Each capability call in an agent run | mutable. `status` pending/awaiting_approval/denied/approved/executing/succeeded/failed/timed_out/skipped, `risk_level` | `run_id`, `agent_run_id`, `plugin_installation_id`, `capability_id`, artifact id lists | position 6; exported | `agents.run` | none |
| `artifacts` (`Artifact`, `artifact_id`) | workspace. Output metadata of a run | mutable. `sensitivity`, `retention_class` ephemeral/standard/regulated/locked, `status` available/deleted, `content_ref` | `run_id`, `agent_run_id`, `created_by_agent_run_id` | position 8; exported (metadata only; `content_ref` kept) | `artifacts.read` | none |
| `conversation_threads` (`ConversationThread`, `thread_id`) | workspace | mutable. `status` open/closed | `agent_id` | position 5; exported | via task and agent permissions | none |
| `tasks` (`Task`, `task_id`) | workspace. Product-level unit of user work, may be private | mutable. `audience` workspace/private (`audience_owner_principal_id`), `status` open/completed/archived, `active_turn_id`, `latest_turn_id`, `private_skill_snapshots`, idempotency key column | `agent_id`, `agent_version_id`, `thread_id`, `creation_message_id`, `active_turn_id`, `created_by_principal_id` | position 4; exported (others' private tasks withheld) | `tasks.view`, `tasks.create`, `tasks.manage`, plus `agents.run` | idempotency columns; paging on `created_at` column |
| `task_messages` (`TaskMessage`, `message_id`) | workspace | immutable. `role` user/assistant, `content` (assistant content truncated to 32,000 chars by the worker) | `task_id`, `turn_id`, `created_by_principal_id` | position 3; exported. Grows unbounded | as tasks | `task_id` |
| `task_turns` (`TaskTurn`, `turn_id`) | workspace. One user request and its execution lease | mutable. `status` queued/running/succeeded/failed/cancelled; `attempts`, `worker_id`, `run_idempotency_key`, `agent_run_id` | `task_id`, `user_message_id`, `assistant_message_id`, `agent_run_id` | position 2; exported | as tasks; claimed by the worker under scope `queue` | `status` |
| `canvas_runs` (`CanvasRun`, `run_id`) | workspace. Workflow-studio run state | mutable | `job_id`, `workflow_id`, `workflow_version_id` | position 11; not exported | `workflows.view/manage/run` | none |
| `canvas_settings` (`CanvasSettings`, `workflow_id`) | workspace. Studio settings, one row per workflow | mutable | `workflow_id`, `principal_id` | position 32; not exported | `workflows.manage` | none |

### 2.4 Models and providers

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `model_providers` (`ModelProvider`, `id`) | organization. Customer-owned provider connection | replace. `provider_type` openai/anthropic/gemini/azure_openai/bedrock/openai_compatible/local, `status` active/disabled, `auth_ref` | `auth_ref` to `product_secrets.secret_ref` (the provider owns the secret and deletes it) | position 29; exported without `auth_ref` | read `models.view`, write `models.manage` | none |
| `model_profiles` (`ModelProfile`, `id`) | organization | mutable. `status`, `model_traits`, optional input/output rates and per-run cost cap | `provider_id` to `model_providers`, `fallback_model_profile_id` | position 28; exported | `models.view` / `models.manage` | none |
| `model_configurations` (`OrganizationModelConfiguration`, `org_id`) | organization. One row per organization; the record id is the org id | mutable. `mode` customer_managed/weaves_managed (may be unset) | `default_provider_id`, `default_model_profile_id` | position 30; exported | `models.manage` | none |
| `organization_limits` (`OrganizationLimitOverride`, `org_id`) | organization. One row per organization; the record id is the org id | mutable. Optional caps (`max_queued_per_org`, `requests_per_minute_org`, `seats`, `schedules`, `agents`, `workflows`, `tool_calls_per_day`, `storage_tasks`), `updated_by`, `reason` | none | removed by the purge sweep; not exported | operator script only (owner connection); the organization's own role can read the row | none |

`mode` can be `weaves_managed`, which presumably marks platform-provided models and
is a likely hook for phases 5 and 7; there is currently no usage, quota or billing
collection.

### 2.5 Integrations and plugins

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `plugins` (`PluginDescriptor`, `plugin_id`) | mixed catalog. The contract has no `org_id`, but the column is set: three built-ins (`github`, `slack`, `jira`) have `org_id NULL`; custom (MCP) descriptors are owned by one organization (`PostgresPluginCatalog.create_owned_descriptor`). Ownership is explicit in `product_plugin_ownership` once migration 20 is activated | immutable contract (frozen). `status` active/deprecated/disabled, `capability_manifest` 1..200 entries | none outward. `plugin_installations.plugin_id` refers to it | position 26; owned descriptors deleted, built-in and quarantined ones are guarded by a trigger and skipped by purge. Not exported | `plugins.view`; `plugins.manage` to install | `plugin_id` (installations index) |
| `plugin_installations` (`PluginInstallation`, `plugin_installation_id`) | workspace. A connector configured in a workspace | mutable. `status` active/disabled/error, `enabled_capability_ids`, `auth_ref` | `plugin_id` to `plugins`; `auth_ref` to `product_secrets` | position 25; exported without credentials | `plugins.view` / `plugins.manage` | `plugin_id` |
| `oauth_authorization_states` (`OAuthAuthorizationState`, `oauth_state_id`) | workspace. In-flight connector OAuth setup | immutable (frozen). `provider` github/slack/jira/mcp, `step`, `secret_ref` | `principal_id`, `secret_ref` | position 27; never exported; read by the callback under scope `oauth` | `plugins.manage` | none |

### 2.6 Skills

Skill content is versioned: a definition points at an immutable version, publication
and revocation are separate immutable facts.

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `skill_definitions` (`SkillDefinition`, `skill_id`) | organization. `scope` personal (owned by a principal) or organization | mutable. `slug`, `status` active/archived, `current_version_id` | `owner_principal_id`, `maintainer_principal_ids`, `current_version_id` | position 24; exported (others' personal skills withheld) | read `skills.view`; write `skills.personal.manage`, `skills.org.publish`, `skills.org.manage` | `slug`, `scope`, `owner_principal_id` |
| `skill_versions` (`SkillVersion`, `skill_version_id`) | organization | immutable. `version` number, `content_digest` | `skill_id`, `required_capability_ids`, `author_principal_id` | position 23; exported | as definitions | `skill_id`, `version` |
| `skill_publications` (`SkillPublication`, `publication_id`) | organization | immutable. `self_review_confirmed` must be true | `skill_id`, `skill_version_id`, `publisher_principal_id` | position 22; exported | `skills.org.publish` | `skill_version_id` |
| `skill_revocations` (`SkillRevocation`, `revocation_id`) | organization | immutable | `skill_id`, `skill_version_id`, `revoked_by_principal_id` | position 21; exported | `skills.revoke` | `skill_version_id` |

### 2.7 Audit

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `audit_events` (`AuditEvent`, `audit_event_id`) | organization or workspace according to `target_scope`; `workspace_id` is null for organization-level events | immutable. `action` and `target_type` match `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`, `metadata` free JSON, `created_at` is also the column used for paging | `actor_principal_id`, `target_id` (a free-form id of any collection), `request_id`, `correlation_id` | kept: purge removes all events except actions `organization.lifecycle.*` and `organization.exported`; exported | read `audit.read`; written by services in the same unit of work as the change | none (column indexes: `org_id`, `workspace_id`, `created_at`, `record_id`) |

### 2.8 Execution queue and evaluations

| Collection (contract, id) | Scope and why | Mutability / key fields | References (lifetime owner) | Purge / export | Read / write permission | JSON paths in indexes |
|---|---|---|---|---|---|---|
| `execution_jobs` (`ExecutionJob`, `job_id`) | workspace. Durable queue entry for a workflow or agent run; claimed with `FOR UPDATE SKIP LOCKED` | mutable. `status` queued/running/cancel_requested/succeeded/failed/cancelled, `attempts` 0..100, `worker_id`, `idempotency_key`, `task` text up to 12,000 | `agent_id`, `workflow_id`, `workflow_version_id`, `thread_id`, `trigger_id`, `schedule_id`, `evaluation_id`, `retry_of_job_id`, `run_id` | position 1 (first; running jobs are set to `cancel_requested`, queued ones to `cancelled`); not exported | `agents.run`, `workflows.run`; `users.manage` to act on another person's job | `status` |
| `evaluation_suites` (`EvaluationSuiteRecord`, `suite_id`; tenant-keyed) | workspace. Client-named suites | mutable | `created_by_principal_id` | position 14; not exported | read `agents.run`, write `agents.manage` | none |
| `evaluation_executions` (`EvaluationExecution`, `evaluation_id`) | workspace | mutable. `status` queued/running/cancel_requested/succeeded/failed/cancelled | `suite_id`, `agent_id`, `execution_job_id` | position 12; not exported | `agents.run` | none |
| `evaluation_reports` (`EvaluationReportRecord`, `evaluation_id`; tenant-keyed) | workspace | immutable | `suite_id`, `agent_id`, `submitted_by_principal_id` | position 13; not exported | read `artifacts.read` | none |

### 2.9 Names that exist in the database layer but have no repository

### Tenant limits (migration 25)

Per-organization limits are resolved from the `WEAVES_LIMIT_*` environment plus an optional operator override
row in `organization_limits` (`OrganizationLimitOverride`, record id = org id, one row per organization, written
only by `infra/gcp/singlevm/organization_limits.py` on the owner connection; removed with the organization by
the purge sweep; not exported). Admission counts the stored rows inside the inserting transaction under
`pg_advisory_xact_lock("limit:{org}:{resource}")`, so there is no counter table. Migration 25 adds three partial
indexes for those counts (`limits_turn_queued_org_idx`, `limits_job_queued_org_idx` on queued `task_turns` and
`execution_jobs` by `org_id`, and `limits_tool_invocation_org_idx` on `tool_invocations (org_id, created_at)`) and
the `SECURITY DEFINER` function `weaves_global_queued_work()`, which returns only the global queued count for the
emergency brake without giving the restricted role any cross-tenant row access.

### Public waitlist (migration 24)

The waitlist stores two global collections in `product_records` with `org_id NULL`. Because the
tenant policy lets null-org rows through, migration 24 (`infrastructure/database/waitlist_schema.py`)
ships with a policy change (the repeatable `POLICY_FUNCTIONS`): `weaves_rls_allows` first routes the
two waitlist collections to `weaves_system_allows`, `bootstrap` no longer allows them, and a new
`waitlist` purpose allows exactly them and nothing else (it sees no other row, even null-org ones).
Indexes: `waitlist_entry_email_uq`, `waitlist_submission_token_uq`, `waitlist_submission_entry_idx`,
`waitlist_submission_created_idx`. The public routes (`/api/v0/waitlist/*`) are outside sign-in, cookies
and CSRF and never touch accounts, organizations, users, sessions or the allowlist. Question sets and
consent texts are immutable, locked files under `contracts/waitlist` (`published.lock.json`). Rate
limiting reuses `product_login_throttle` with hashed buckets (no address is stored).

### Slack access (migration 33)

The Slack bot's access policy (design: `docs/slack-bot-design.md` 3.3 and 3.5) adds three
organization-scoped collections in `product_records`, so they inherit row-level security by
`org_id`, are removed by the generic purge sweep (positions after `plugins`) and are exported
(without any Slack message text):

| Collection | Scope and key | Fields | Access |
|---|---|---|---|
| `slack_access_settings` (`SlackAccessSettings`, `org_id`) | organization; one row, record id = org id; absent until the first change (defaults apply) | mutable. `settings_version` (bumped by every settings, pause or allowlist change), `ask_enabled`, `connector_audience` (installation id to `everyone`/`allowed_people`/`not_in_slack`, default `not_in_slack`), `partial_answers`, `planner_profile_id`, `ask_limits`, `rules` (`create_task`, `pr_review`, `run_workflow`, `ask_private`: `off`/`user_only`/`channel_only`/`user_and_channel`/`user_or_channel`, default `off`), `default_task_agent_id` (the agent that runs `task:` requests; validated against the organization's agents, an older client that omits it keeps the stored value), `execute_limits` (`user_per_10_min` 10, `channel_per_10_min` 30), `workflow_ids`, `pr_review_repos`, `paused`, `qa_principal_id`/`qa_agent_version_id` (set by the Ask slice), `workspace_urls` (team id to `https://<name>.slack.com/`, a cache the S5 slice fills from `auth.test`, never editable and without a version bump) | `slack.manage` (organization-level binding) |
| `slack_allowed_users` (`SlackAllowedUser`, `id` = `{org_id}:{slack_user_id}`) | organization | mutable. `slack_user_id`, `principal_id` (administrator-asserted member mapping; null = listed by Slack id only), `note`, `added_by`. Unique per org by `slack_user_id` (`slack_allowed_users_org_user_uq`) | `slack.manage` |
| `slack_allowed_channels` (`SlackAllowedChannel`, `id` = `{org_id}:{channel_id}`) | organization | mutable. `channel_id`, `name_cached`, `added_by`. Unique per org by `channel_id` (`slack_allowed_channels_org_channel_uq`) | `slack.manage` |

The Slack request pipeline (S4a, `modules/slack/pipeline/`) adds one more organization-scoped
collection, no migration:

| Collection | Scope and key | Fields | Access |
|---|---|---|---|
| `slack_connector_health` (`SlackConnectorHealth`, `id` = the plugin installation id) | organization; one row per connector installation, a cache | mutable. `installation_id`, `status` (`healthy`/`error`/`token_expired`/`rate_limited`/`disabled`), `reason_code`, `checked_at`. Rewritten every ten minutes by the worker's `slack_health` tick and by a live re-probe (3 s) of a needed tool whose snapshot is stale; never a token or connector data. Removed by the generic purge sweep and exported | system job only (the tick binds each organization's own tenant context) |

The Ask flow (S4, `modules/slack/dispatch/` and `modules/slack/reply/`) registers
`slack_threads` (unique index of migration 33, no new migration) and adds two inbox operations:

| Collection | Scope and key | Fields | Access |
|---|---|---|---|
| `slack_threads` (`SlackThread`, `id` = `{org_id}:{team_id}:{channel_id}:{thread_ts}:ask`) | organization; one row per Slack thread and kind (`slack_threads_key_uq`) | mutable. Thread level: `task_id` (the hidden Q&A task), `requester_*`. Latest request: `turn_id`, `event_id`, `request_id`, `message_ts`, `requested_at`, `request_count`, `run_state` (`queued`/`running`/`cancel_requested`/`done`/`failed`/`stopped`), `status_ts` (working message, or `native`), `status_text_key`, `answer_ts` (or `private`), `reply_state` (`pending`/`posted`/`undeliverable`), `reply_attempts`, `next_attempt_at` (backoff and the 45 s posting lease), `reply_error` (a code), `posts`, `private_sent`, `finished_at`. Ids, timestamps and state codes only: never message text, an answer, a token or connector data. A `task` row (kind `task`, tier `execute`, id ends in `:task`) belongs to a normal task and is never swept by the retention tick, which only deletes `ask` rows (hidden Q&A tasks) 30 days after the thread's last activity (`slack_retention` tick); removed by the organization purge sweep and exported | the three Slack worker ticks only (each binds the organization's tenant context first) |

Reaction feedback (S4c, `modules/slack/feedback/`) registers `slack_feedback` (unique index of
migration 33, no new migration):

| Collection | Scope and key | Fields | Access |
|---|---|---|---|
| `slack_feedback` (`SlackFeedback`, `id` = `{org_id}:{turn_id}:{asker_ref}`) | organization; one row per answered turn and asker (`slack_feedback_turn_asker_uq`), the latest reaction wins | mutable. `answer_ref` (`{channel}:{answer ts}`), `turn_id`, `task_id`, `agent_run_id`, `request_id` (`slack-{event_id}`), `asker_ref` (the mapped member's principal id, else `hmac:` plus 32 hex characters of HMAC-SHA256 of the Slack user id keyed with a per-organization pepper), `asker_kind` (`member`/`hashed`), `value` (`1`/`-1`), `source` (`slack`; `web` is reserved), `at` (when the reaction event was received), and a snapshot of the run: `agent_id`, `model_id`, `connector_set` (sorted capability ids; empty is general knowledge). Never message text, a reason, a raw Slack user id or a token. Kept `WEAVES_USAGE_RETENTION_DAYS` (400) days by the `slack_feedback_retention` tick; removed by the organization purge (`PURGE_ORDER`, before `slack_threads`); exported | the `slack_feedback` worker tick (binds the organization's tenant context); the named system purpose `feedback` (this collection only) for the retention delete and the operator report |

The pepper is a random secret in the secret store (purpose `slack_feedback_pepper`); its reference is
`slack_access_settings.feedback_pepper_ref`, set without bumping `settings_version` and never returned
by the settings API. A mapped member and an unmapped person never share a row: when a Slack user is
mapped later, their next reaction replaces the hashed row with a member row.

`slack_event_inbox` rows leave `pending` through `settle_event` (a compare-and-set to `done` or
`dropped`, which also clears the stored question text); `list_events` can filter by `event_type`
so the Ask dispatcher claims only `app_mention` rows and the S4c feedback tick claims only `reaction_added` rows.

`tasks` gains an optional `origin` (`TaskOrigin`, S5: `kind` `slack`, `channel_id`, `channel_name`, `requested_by_name`, `permalink`; the public summary of a Slack-created task, with no Slack team, user, thread or event id) and filters `GET /tasks?origin=slack` over `payload->'origin'->>'kind'`. `task_turns` gains an optional `origin` (`TurnOrigin`: `kind` `slack`/`slack_qa`, team, channel,
thread, asker and event ids, `action`, optional `repo`/`workflow_id`, the approved
`capability_ids` and the guard's `verdict`; never message text). It is part of the turn's JSON
payload, so there is no migration; turns without it behave exactly as before. The planner's usage
ledger row uses reservation key `{run idempotency key}:plan` (at most 128 characters).

Migration 33 (`infrastructure/database/slack_access_schema.py`) also creates the partial unique
indexes the Ask slices will use (`slack_threads_key_uq`, `slack_feedback_turn_asker_uq`; those
collections are registered later) and idempotently adds the permission `slack.manage` to stored
`roles` whose `system_role_key` is `admin` or whose name is "Organization owner". The Admin preset
grants every `Permission`, and `seed_standard_roles()` rewrites stored Admin roles at startup, so
organizations created or seeded later get it without the migration. Audit actions written:
`slack.access.settings_updated`, `slack.access.paused`, `slack.access.resumed`,
`slack.access.user_added|user_updated|user_removed` (the assertion of a Slack id to a member),
`slack.access.channel_added|channel_removed`, and per request `slack.decision` (plus
`slack.execute.allowed|blocked`), none of which carries message text. Allowed Ask decisions are
the rows Ask rate limits count (`target_type` `slack_ask_allowed`, rolling 10 minutes and 1 day).

### Slack installations and event inbox (migration 32)

Two real tables (`infrastructure/database/slack_schema.py`, ports in `application/ports/slack.py`,
stores in `infrastructure/persistence/slack_store*.py`), because an event names only a `team_id`
before any organization is known and the inbox needs a true unique key.

`slack_installations` has one row per (organization, Slack workspace): `installation_id` (primary key),
`team_id`, `org_id`, `workspace_id`, `team_name`, `enterprise_id`, `app_id`, `bot_user_id`,
`bot_secret_ref` (an opaque reference into `product_secrets`, purpose `slack_bot_token`; the token itself is
never in this table, a response or a log), `scopes`, `installed_by_principal_id`, `installed_by_name`,
`installed_by_slack_user`, `status` (`active`, `revoked`, `uninstalled`, `suspended`), `installed_at`,
`updated_at`, `revoked_at`. Indexes: `slack_installations_team_held_uq` (unique `team_id` while the status is
`active` or `suspended`: a workspace belongs to exactly one organization), `slack_installations_org_team_uq`
(one row per organization and workspace; a reinstall updates it and rotates the secret), and
`slack_installations_org_idx`. Policy: a tenant sees only its own rows; the system purpose `slack` sees all.

`slack_event_inbox` has `event_id` (primary key: Slack retries insert nothing), `team_id`, `org_id`,
`installation_id`, `event_type` (`app_mention`, `reaction_added` for thumbs only), `state`
(`pending`, `processing`, `done`, `dropped`), `drop_reason`, `attempts` (Slack retry number plus one),
`request_id` (`slack-{event_id}`), `channel_id`, `user_id`, `message_ts`, `thread_ts`, `text` (the mention
only, capped at 4,000 characters, never set on a dropped row, never logged) and `reaction`, `received_at`.
`slack_event_inbox_message_uq` (unique `team_id, channel_id, message_ts` for `app_mention`) stops one
message becoming two rows under different event ids. Only the system purpose `slack` can read or write it;
no tenant can.

Lifecycle (S7, no new migration; `modules/slack/lifecycle.py`, `purge.py`, `retention.py`). Every way a
workspace stops being served (Disconnect, `app_uninstalled`, a bot `tokens_revoked`, a dead-token reply,
quarantine with credential revocation, restore reconcile) runs `SlackLifecycle.end`: the 6.2 tombstone
`slack_installation_ended` (`subject_kind` = `uninstalled` or `revoked`, `subject_id` =
`{installation_id}@{installed_at}`, one per install generation) is written first and a failed write stops
everything; then the status flips (freeing `team_id`), the team's pending inbox rows become `dropped` with
the text erased (`drop_reason` `installation_ended`), the token is revoked at Slack (only when Weaves
ends it) and deleted (its own `secret_deleted` tombstone first), and `bot_secret_ref` is cleared. A crash
in between leaves an ended row that still names its token; the `slack_lifecycle` worker tick sweeps it.
Quarantine marks active installations `suspended` (reversible; release restores exactly those) and, with
`--revoke-credentials`, ends them for good. Organization purge has a `slack` step before the secret
purge (tombstone, `auth.revoke`, secret delete, then the row; bounded, idempotent, crash-resumable) and
removes the organization's inbox rows; the access settings and allowlists are in `PURGE_ORDER`, and the
thread and feedback collections (S4) are removed by the generic sweep. Restore reconcile re-ends
installations whose tombstone generation matches the restored row, so an old dump cannot bring back an
uninstalled workspace or a revoked token. The owner export lists installations (status, workspace,
installer, scopes; never a token or secret reference) under `slack_installations`. Inbox retention: rows
are deleted 7 days after receipt once done or dropped, and unprocessed rows after 14 days (ASSUMPTION), in
batches of 500 (at most 10 batches per 10-minute tick).

### Removed: approval policies and approval requests (migration 31)

The "Approvals & Policy" and "Artifacts & Audit" app sections were removed (owner decision,
2026-11-25). Migration 31 (`infrastructure/database/approvals_removal.py`) deleted every
`approval_policies` and `approval_requests` row, the `tool_invocations` rows that carried an
`approval_request_id` (only the retired decision flow wrote them), and the retired keys
`tool_invocations.approval_request_id`, `agent_versions.approval_policy_id` and the permission
`approval_policies.manage` inside stored `roles`; it dropped `approval_requests_page_idx`,
`approval_requests_pending_idx` and `tool_invocations_approval_idx`. Kept: `audit_events` (still
written by every service and readable at `GET /api/v0/audit-events[/export]`, `audit.read`),
`artifacts`, and workflow approval steps (state inside `workflow_runs`, decided in the run view with
`actions.approve`). Images older than migration 31 refuse this database
(`min_rollback_schema_version` is 31).

### Platform controls, operator audit and quarantine (migration 30)

`platform_controls` is a single row (`id = 1`): `admissions_paused`, `worker_paused`, `read_only`,
`signup_enabled` (nullable booleans; NULL means the environment default applies, so an untouched
database behaves exactly as before), `message`, `version`, `updated_by`, `reason`, `updated_at`. API
and worker processes read it through a 5 second cache (`modules/platform_controls/controls.py`);
a failed read keeps the last value read. Row security: everyone may `SELECT`, nobody but the owner
role may write.

`operator_actions` is append-only (triggers reject `UPDATE`, `DELETE` and `TRUNCATE`, even for the
owner): `action_id`, `at`, `operator`, `command`, `target`, `org_id`, `reason`, `ticket`,
`correlation_id`, `result` (`ok` or `refused`) and `details` (identifiers and counts only, never
secrets or tenant content). Row security is on with no policy, so the restricted application role
sees and writes nothing. Every mutating operator script writes a row, including the 5.1
`organization_limits` changes and every lifecycle command.

`org_quarantines` has one row per organization: `state` (`quarantined`, `released`), `changes` (the
ids of schedules and webhook triggers it disabled, whether it suspended the organization, the
credentials it revoked), who, why, `correlation_id` and timestamps. Release re-applies only
`changes`. Same row security as `operator_actions`. Quarantined organizations are still ordinary
organizations: the stop itself is the existing lifecycle suspension.

### Usage ledger (migration 27)

`usage_ledger` has one row per model call, primary key `(org_id, reservation_key)`; the key is the
`agent_run_id` (each agent run makes at most one provider request, already deduplicated by the run
idempotency index). The one exception is the Slack Ask planner (S4a): its tool-less call runs
before the agent run exists and has its own row keyed `{run idempotency key}:plan` (at most 128
characters, `agent_run_id` and `run_id` hold the same key; the reconciler decides these rows by age
alone). Columns: `workspace_id`, `agent_run_id`, `run_id`, `agent_id`, `provider_id`,
`model_id`, `state` (`reserved`, `settled`, `released`, `unknown`), `reserved_input_tokens`,
`reserved_output_tokens`, `reserved_cost_nano_usd`, `input_tokens`, `output_tokens`,
`estimated_cost_nano_usd`, `outcome`, `reserved_at`, `settled_at`, `updated_at`. Money is integer
nano-USD (1 USD = 10^9), never a float, and null when the model profile has no price.
`usage_daily` is the rollup `(org_id, day, agent_id, provider_id, model_id)` with `calls`,
`input_tokens`, `output_tokens`, `estimated_cost_nano_usd` and `unpriced_calls`; it is updated in the
same transaction that settles the ledger row, and only by the first settlement of a row.
Both tables have row security (`tenant_isolation`): `org_id` equals `app.org_id`, or the named system
purpose `usage` (reconciler, retention, operator report). The `usage` purpose also reads
`workflow_runs`, `agent_runs`, `execution_jobs`, `organization_lifecycle`, `organizations` and
`workspaces` in `product_records`, and nothing else.

States: the call site reserves before the provider request (reserved tokens = prompt characters
divided by `WEAVES_USAGE_RESERVATION_INPUT_CHARS_PER_TOKEN` plus `max_output_tokens`). `settled`
means the provider reported tokens, including for outputs later rejected (schema invalid, over
budget) and for unusable answers whose token counts are known. `released` means no request was
processed (disabled model, missing credential, HTTP 4xx other than 408). `unknown` means the request
may have reached the provider (timeout, connection loss, HTTP 5xx or a worker crash) and its tokens
are not known; the reservation stays as the upper bound for admission and is never counted in the
rollup. Settlement is an upsert on the key that only transitions `reserved`, `unknown` or `released`
rows, so a replay or retry is a no-op and a late real settlement upgrades `unknown` exactly once.
The worker's `usage_reconcile` tick settles a `reserved` row from the run record when it already
holds tokens, or marks it `unknown` once its lease expired: the workflow run is no longer running,
its execution job stopped heartbeating (the 120 s job fence), or the run passed the run recovery
fence. There is no time-to-live of its own. Under `WEAVES_LIMITS_MODE=enforce` a reservation that
would take settled plus outstanding usage over the organization's day/month token cap or estimated
daily cost cap is refused with `limit_exceeded` (advisory lock `usage:{org}`); in `observe` it is
logged and recorded. Retention (`WEAVES_USAGE_RETENTION_DAYS`) applies to live organizations only;
organization deletion removes the ledger and rollup rows (purge step `usage`), and the retention
tick also sweeps rows a model call that was still in flight wrote after its organization was purged.
The owner export includes `usage_daily`. Customers use their own provider keys (BYOK): this records
usage, it does not charge.

### Organization slug and email domain (migration 23)

`organization_settings.slug` and `.email_domain` live in the tenant-owned settings payload
and are made globally unique by two partial unique expression indexes
(`organization_slug_uq`, `organization_email_domain_uq`, on the lowercase value). Migration 23
(`infrastructure/database/organization_identity.py`) backfills existing organizations
idempotently: slugs from the name (numeric suffix on collision or reserved word); an email
domain only when the explicit owner's verified email is on a custom domain and exactly one
organization would claim it (ambiguity is left unset and logged, never guessed).

- Slug: lowercase `[a-z0-9-]`, 3..40, no leading, trailing or double hyphen; reserved:
  admin api app www weaves support help login signin signup account settings static assets new.
  Reserved atomically in the creation unit of work (advisory lock, then the index as backstop).
  It is an identifier only: nothing is routed by it. A deleted organization's slug and domain
  are freed when purge completes, because the settings row is purged.
- Email domain rule: an organization owns its custom email domain. A person with a verified
  email on a claimed domain cannot create any organization (409 `domain_belongs_to_organization`),
  whether or not they are a member. Public mailbox providers (`shared/organization_naming.py`,
  plus env `WEAVES_PUBLIC_EMAIL_DOMAINS`) claim nothing; `WEAVES_ENFORCE_ORGANIZATION_DOMAINS=false`
  switches enforcement off. Changing or adding domains after creation is out of scope.
- Account views: each membership adds `organization_slug`, `is_owner` and `role_label`
  (Owner, else Admin > Builder > User, else first custom role name, else empty) and the payload
  adds `organization_creation` with the reason creation is or is not allowed.

Migration 22 (`infrastructure/database/legacy_purge.py`) deleted every row of the
removed collections `user_passwords`, `user_password_credentials`, `user_sessions`,
`user_invitations`, `sso_connections`, `sso_external_identities`,
`email_login_challenges`, `account_login_challenges`, `memory_items` and
`knowledge_documents`, dropped their indexes and the `sso_identity_immutable` trigger
with its function, and removed the names from `PURGE_ORDER`. Every collection stored in
`product_records` is now in `REPOSITORY_SPECS`; the test
`tests/integration/test_collection_drift.py` enforces it, and `PURGED_COLLECTIONS` lists
the removed names. Phase 8 (enterprise identity), if it revives `sso_*`, designs its
constraints afresh. Purge still handles any unknown collection through
`delete_remaining_batch`.

Collections not exported (by design or omission): `organizations` and
`organization_settings` and `organization_lifecycle` (included as document sections),
`account_*`, `api_credentials`, `principals`, `oauth_authorization_states`, `plugins`,
`execution_jobs`, `evaluation_*`, `canvas_*`. The export module documents the
exclusion of secrets, credentials and sessions only; the omission of evaluations,
canvas data and execution jobs is not stated there.

## 3. ER diagrams

See [data-model/er-diagrams.md](data-model/er-diagrams.md): an overview and one
diagram per domain. The relationships are logical (field references), not foreign
keys.

## 4. Physical schema

See [data-model/physical-schema.md](data-model/physical-schema.md): tables, columns,
indexes with the queries they serve, triggers, functions, row-level security
policies and grants, the migration history, and "Observations for the review".

## 5. Invariants and consistency rules

Where each rule is enforced: **code** (service layer), **repo** (repository SQL),
**index**, **policy** (row-level security), **trigger**.

| Rule | Enforced by | Source |
|---|---|---|
| Record identity `(collection, record_id)` is unique | primary key | migration 1 |
| Ownership of a record never changes: a `put` that would change `org_id` or `workspace_id` of an existing row updates nothing and raises `OwnershipConflict` | repo (`ON CONFLICT ... DO UPDATE ... WHERE org_id IS NOT DISTINCT FROM EXCLUDED.org_id AND workspace_id IS NOT DISTINCT FROM ...`) | `postgres_repository.py::put` |
| Compare-and-set writes (`put_if_*`) also pin `org_id` and `workspace_id` | repo | `postgres_repository.py` |
| Scope predicate: scoped reads filter `org_id` (and `workspace_id`) in SQL; unscoped `get`, `list`, `find_by_field`, `list_by_field`, `delete`, `consume` rely on the row policy for tenant isolation | repo for scoped reads, policy for the rest | `postgres_repository.py`, `row_security.py` |
| Tenant row visibility: a row is visible when `org_id = app.org_id`, when `org_id IS NULL`, or when the named system purpose lists the collection; account collections and the two waitlist collections only by system purpose (`waitlist` for the latter; the `waitlist` purpose sees nothing else) | policy `tenant_isolation` (only when `WEAVES_DB_APP_ROLE` is set; the policy is not forced on the table owner) | `row_security.py` |
| Tenant keys: `evaluation_suites` and `evaluation_reports` are stored as `org:id` | repo | `postgres_repository.py::_record_id` |
| Idempotency: per `(org, workspace, requested_by_principal, idempotency_key)` for `workflow_runs`, `execution_jobs`, `tasks`, `task_turns`; a conflicting create raises `IdempotencyKeyAlreadyExists` and the service replays or raises `IdempotencyConflict` when the request content differs | index (4 partial unique indexes) plus code (fingerprint compare) | migrations 1, 14; `modules/tasks/creation_persistence.py` |
| Per-account organization-creation idempotency and quota: id is `sha256(account_id, key_digest)`; unique `(account_id, key_digest)`; quota counts rows, default 3 (`WEAVES_ACCOUNT_ORGANIZATION_LIMIT`) | index plus code (`_enforce_quota` counts `list_by_field("account_id")` under `account-organization:{account_id}` advisory lock) | `account_organizations.py`, `account_schema.py` |
| One verified identity belongs to one account; an account holds an identity once | partial unique indexes `account_identity_verified_uq`, `account_identity_owner_uq` | `account_schema.py` |
| At most one membership per account and organization, and per tenant-local user | unique indexes `membership_account_org_uq`, `membership_user_uq` | `account_schema.py` |
| Session, OAuth state, API token, webhook secret digests are unique | unique expression indexes | migrations 1, 10; `account_schema.py` |
| Active user email is unique per organization (case-insensitive) | unique index `product_records_active_user_email_uq` (not for status `deleted`) | migration 8 |
| One system role key per organization; one role binding per `(org, principal, role, workspace-or-empty)` | unique indexes | migrations 1, 13 |
| Skill uniqueness: slug per organization (organization scope), slug per owner (personal), version number per skill, one publication and one revocation per version | unique indexes | migrations 17, 18 |
| Explicit plugin ownership record: each `plugins` row has exactly one `product_plugin_ownership` row (`builtin`, `owned` with org, `quarantined` with reason); ownership, type and identity are immutable; built-in and quarantined descriptors cannot be deleted; installations must match the descriptor's owner and the installation's workspace must belong to its organization | triggers, check constraint, foreign key | migration 20, `plugin_ownership_sql.py` (only after operator activation) |
| Lifecycle state machine: ACTIVE, SUSPENDED, DELETION_REQUESTED, DELETING, DELETED. Owner can request deletion (exact name confirmation) and cancel within grace; operator can suspend, resume, request, cancel, purge now; worker moves DELETION_REQUESTED to DELETING when the grace deadline passes and finishes to DELETED after two consecutive clean passes under the administration lock; DELETED is terminal | code (`organization_lifecycle.py`, `organization_purge.py`); `Organization.status` is always written with the lifecycle record in one unit of work | `lifecycle_state.py`, `organization_lifecycle.py` |
| An organization always has an active human owner/admin; ownership changes are serialised | code under `weaves-org-administration:{org}` advisory lock | `organization_guard.py` |
| No membership is created into a non-ACTIVE organization | code under the same lock | `account_linking.py::_create` |
| Secrets: owned by the creating organization; readable only with a matching `org_id`; unowned secrets unreadable | policy on `product_secrets` plus `AND org_id = ...` in SQL | `infrastructure/secrets/store.py` |
| Sign-in attempts: fixed-window counters | row per bucket, `CHECK attempts > 0` | migration 9, `infrastructure/auth/throttle.py` |
| Skill, agent, workflow versions and task messages are immutable | code (frozen pydantic models; repositories never update them) | `contracts/v1/base.py` |
| Contract validity of every document | read-time strict parse (`model_validate_json`) | `postgres_repository.py` |

### 5.1 What is not enforced by the database (gaps for the architect)

1. No foreign keys: references between collections (section 2) can dangle; the
   only reference check in the database is the plugin trigger set (post migration 20).
2. No type, nullability or enum checks for `payload`: a row can hold any JSON. Only
   the application's read path rejects invalid shapes, and only when the record is read.
3. `org_id` / `workspace_id` columns are not validated against `payload.org_id` /
   `payload.workspace_id`. They are copied by the repository (`_scope`), so a direct
   SQL writer could make them diverge. (The plugin installation trigger does check
   equality for `plugin_installations`.)
4. Workspace belongs to the organization: not checked for any collection except
   `plugin_installations` (trigger). Any `workspace_id` string is accepted.
5. Row-level security is inert unless `WEAVES_DB_APP_ROLE` is set; owner connections
   (migrations, operator scripts) bypass it. The table is not `FORCE ROW LEVEL SECURITY`.
6. `org_id IS NULL` rows are visible to all tenants, except the account and waitlist
   collections; nothing prevents a tenant-owned collection from being written without an org.
7. Immutability of versions, frozen records and audit events is by convention. The
   only immutability triggers are the plugin descriptor/ownership guards (after
   migration 20). There is no protection against `UPDATE` or `DELETE` of
   `audit_events` for a role with `DELETE` privilege (the app role has full DML on
   every table, see physical-schema).
8. Lifecycle state machine transitions and `Organization.status` / lifecycle
   projection consistency are code only.
9. The invariant "at least one active owner" and quota counts are advisory-lock
   protected in code; a writer that skips the lock can violate them.
10. Uniqueness that is *not* present: `workspaces.name` per organization,
    `agents` / `workflows` names, `model_providers` display names, `plugin_installations`
    per `(workspace, plugin)`, `principals.user_id` (one principal per user is
    assumed by `find_by_field("user_id")` but not enforced), `users.email` for status
    `deleted`, `workflow_triggers`/`schedules` per workflow.
11. `plugins.record_id` is global across tenants: a custom descriptor id taken by one
    organization is unavailable to every other organization (the catalog raises
    "plugin_id already exists"), and the error reveals existence.
12. Time values inside JSON are ISO strings compared as text (`payload->>'expires_at' <
    cutoff`, `next_run_at <= cutoff`). Correct only while every writer serialises
    timestamps in the same UTC `Z` form; the code normalises the cutoff
    (`.astimezone(UTC).isoformat().replace("+00:00", "Z")`), but an old row in a
    different form would sort wrongly.
13. `product_secrets` may be missing entirely (created lazily when an encryption key
    is configured), and `purge` assumes its presence only through the secret store.
14. No row-count or size limits per tenant at the database layer; limits are in
    contracts (`max_length`) or code.

## 6. Data flows (request to rows)

Tables touched are `product_records` collections unless noted. "Scope" is the
row-level-security context used.

### 6.1 Sign-in with Google

1. `GET start`: `GoogleSignIn` writes `account_oauth_states` (digests of state and
   browser binding, nonce, PKCE verifier, 10-minute expiry). Scope `account`.
2. Callback: `_consume` finds the state by `state_digest` (index `account_oauth_state_uq`)
   and deletes it (`consume`, one use). The provider code is redeemed; the email is
   checked against the allow-list.
3. `GoogleAccountResolution` / `find_or_create_account`: look up a verified
   `account_identities` row by subject (indexes `account_identity_subject_idx`,
   `account_identity_verified_uq`); if none, create `accounts` and
   `account_identities` in one unit of work (a lost race on the unique index discards
   the orphan account).
4. `MembershipLinker.link`: for every `users` row with this email in any organization
   (`list_by_field("email", casefold=True)` through `user_email_lower_idx`) not yet
   linked, take `weaves-org-administration:{org}`, check the organization is ACTIVE,
   create `memberships`, activate an INVITED user (and a disabled principal), and
   write `audit_events` (`account.membership.linked`, `user.activated_by_email`) under
   that organization's own scope. Existing memberships are never modified.
5. `AccountSessions.issue` creates `account_sessions` (stores only the token digest).
   `product_login_throttle` rows count attempts.

### 6.2 Organization selection and each tenant request

`apps/api/authentication.py::_account_tenant_request`: the client sends the account
token and `X-Organization-Id` on every request (there is no stored "current
organization"). Under scope `account` the session is read by `token_digest`
(`account_session_token_uq`), then `AccountOrganizationSelector.select` reads
`memberships` (by account), `organizations`, `users`, `principals`, and
`organization_lifecycle` and `organization_settings` under scope `lifecycle` (to compute the
effective state and, for the narrow deletion-recovery routes, to check the owner).
Any refusal yields one message. Under scope `auth`, `resolve_principal_scope` reads
`principals`, `organizations`, and the principal's `role_bindings` and `workspaces`
to choose the workspace (`X-Workspace-Id` or the first accessible). The request then runs
with `set_request_identity(principal, org, workspace)`, which is what the database
scope uses (`app.org_id`). API credentials (service access) are resolved the same way
from `api_credentials.token_digest` under scope `auth`.

### 6.3 Create an organization atomically

`AccountOrganizationCreator.create` (`modules/auth/account_organizations.py`):
1. Throttle two buckets in `product_login_throttle` (per account and per source address).
2. One unit of work: advisory lock `account-organization:{account_id}`; under scope
   `account` look up `account_organization_requests` by id (replay returns the earlier
   organization if the fingerprint matches, else conflict), enforce the quota
   (count by `account_id`), pick a verified email.
3. `provision_organization` inside the same unit of work creates, under the new
   organization's scope: `organizations`, `organization_settings`, `workspaces`
   (default workspace), `users`, `principals`, `roles` (admin, builder, user presets),
   `role_bindings`, optionally `api_credentials`, `model_providers` (deterministic local
   provider), `model_profiles`, and an `audit_events` row
   `organization.created`. No `organization_lifecycle` row is written (the lifecycle
   service treats a missing record as ACTIVE and creates the row at the first change).
4. Callback `within` (same transaction): `memberships` and the
   `account_organization_requests` row (unique `(account_id, key_digest)`; a unique
   violation means another request won and the whole transaction rolls back).

### 6.4 Task execution

1. Create task (`modules/tasks/creation_persistence.py`): one unit of work inserts
   `tasks`, `conversation_threads`, `task_messages` (user message) and `task_turns`
   (queued). Idempotency is by key and principal; a duplicate create replays.
2. Worker (`apps/worker/main.py`, loop every `WEAVES_WORKER_POLL_SECONDS`, default 1 s):
   under scope `queue`, `claim_next_queued` runs the `FOR UPDATE SKIP LOCKED` CTE on
   `task_turns` (index `product_records_task_turn_claim_idx`) and sets status running,
   increments `attempts`, sets `worker_id`.
3. The worker switches to the claiming tenant's identity, fences the turn
   (`put_if_worker_owned`) and the task (`put_if_task_state`) in one transaction,
   heartbeats `task_turns.updated_at` every 15 s, and runs the agent
   (`AgentExecutionService`): `workflow_runs`, `agent_runs`, `tool_invocations`,
   `artifacts`, `audit_events`.
4. Success: one unit of work creates the assistant `task_messages`, finishes the turn
   and clears the task's active turn. Failure stores a sanitised error and keeps the
   task retryable.
5. Recovery, every 30 s under scope `recovery`: turns with `status = running` and
   `updated_at` older than 300 s are re-queued, or failed after three attempts;
   `workflow_runs`, `execution_jobs` and `tool_invocations` stale rows are failed or
   timed out (section 7 describes the scans these use).
   Separately `execution_jobs` follow the same claim/heartbeat/fence pattern.

### 6.5 Organization deletion and purge

State changes are written as `organizations.status`, `organization_lifecycle` and an
`audit_events` row in one unit of work under the administration lock.
1. Owner requests deletion (exact name): state DELETION_REQUESTED with grace deadline
   (`grace_days_from_env`), organization status becomes suspended. Owner can cancel,
   export (`organization_export.py`, max 50 MB by default, owner only), nothing else.
2. Worker `OrganizationPurgeService.tick` (every `WEAVES_LIFECYCLE_POLL_SECONDS`,
   default 30 s): under scope `lifecycle` reads `organization_lifecycle` by `state`
   (`organization_lifecycle_state_idx`); due organizations move to DELETING.
3. Each pass, under that organization's scope, in order: cancel queued/running jobs and
   queued turns (`cancel_work`); revoke `api_credentials`; delete this organization's
   `product_secrets` (200 at a time); delete the organization's `usage_ledger` and `usage_daily`
   rows (200 at a time); delete each collection in `PURGE_ORDER` in batches
   of 200 (plugin descriptors that are built-in or quarantined are skipped); delete
   `audit_events` except `organization.lifecycle.*` and `organization.exported`;
   sweep any other collection except `KEEP_COLLECTIONS`.
4. A checkpoint (`purge_step`, `purged_rows`, `clean_ticks`) is written to
   `organization_lifecycle`; the passes are idempotent and restartable.
5. After two consecutive passes that delete nothing, under the administration lock:
   delete the organization's `memberships`, require `count_remaining = 0` and no usage rows, then
   `finish_deletion`: state DELETED, `organizations.name` replaced by
   "deleted organization", `deleted_at` set. Kept forever: `organizations`,
   `organization_lifecycle`, `account_organization_requests`, retained `audit_events`,
   and the global account rows.

## 7. Volume and performance notes

No volumes are measured here; the statements below are derived from the code.

Growth without a bound (nothing deletes them except organization purge):
`audit_events` (every administrative action, run and sign-in link), `task_messages`,
`task_turns`, `tasks`, `workflow_runs`, `agent_runs`, `tool_invocations`, `artifacts`
(metadata), `execution_jobs`, `canvas_runs`, `evaluation_executions`,
`evaluation_reports`, `conversation_threads`. There is no
retention or archival job for any of them. Bounded: `account_sessions` and
`account_oauth_states` (cleanup of expired rows in batches of 200 with a 1-hour
grace), `product_login_throttle` (pruned on write by `_maybe_prune`).
`account_organization_requests` grows by design (quota ledger).

Full-collection scans (`.list()` across all tenants, unbounded, executed by the
worker in system scope):

| Call site | Collection | Frequency |
|---|---|---|
| `modules/executions/recovery_service.py::recover_stale_runs` (`self.runs.list()`) | `workflow_runs` | every 30 s |
| `recovery_service.py::_recover_stale_execution_job_records` (`execution_jobs.list()`) | `execution_jobs` | every 30 s |
| `modules/workflows/studio_scheduling.py::tick` (`settings.list()`, `runs.list()`) | `canvas_settings`, `canvas_runs` | every worker loop iteration (about every second when busy; `WEAVES_WORKER_POLL_SECONDS` when idle) |
| `modules/auth/bootstrap.py`, `organization_lifecycle.py::operator_list` (`organizations.list()`) | `organizations` | start-up and operator listing |
| `modules/models/bootstrap.py` (`profiles.list()`, `providers.list()`) | `model_profiles`, `model_providers` | start-up |
| `application/descriptor_catalog.py`, `infrastructure/integrations/plugin_gateway.py`, `runtime_installation_admin.py` (`installations.list()`) | `plugin_installations` | per catalog or gateway call (call sites only; which wiring reaches them with PostgreSQL was not traced) |
| `modules/integrations/oauth_state_service.py::_states.list()` | `oauth_authorization_states` | cleanup of expired states |

Every `.list()` loads all matching `payload` values into memory and parses each
through pydantic. Stale-detection in the first three rows reads all rows and filters in
Python; the queue indexes (`..._task_turn_stale_idx`) exist only for `task_turns`.

Organization-wide scans through `list_scoped(org, workspace)` (indexed prefix by
`(collection, org_id, workspace_id)` but no further filter, whole result in memory) on
growing collections: `agent_runs` (`modules/tasks/worker_service.py::_finish_failure`,
`runtime_run_entrypoint.py`, `recovery_service.py`), `audit_events`
(`runtime_run_entrypoint.py`, `recovery_service.py`, `organization_guard.py`: reading
all events of an organization to find `organization.created` and comparing in Python),
`tool_invocations`, `artifacts`, `execution_jobs`, `evaluation_executions`,
`task_turns` (`execution_summaries.py`), `workflow_runs` (`studio_context.py`). These
are filtered in Python by `target_id`, `run_id` or `action`, so their cost grows with the
organization's history.

Indexed, bounded access paths (the intended model): task list (`list_task_page`, keyset
on `created_at, record_id`), task messages (keyset), audit list (keyset with filters on
`action`, `target_type`, `target_id`, `actor_principal_id` payload fields applied after the
keyset index), queue claim (partial index on queued rows), due schedules (partial index),
stale turn leases (partial index), account sessions and OAuth states by digest, lifecycle by
state, expired-session cleanup by `expires_at` / `revoked_at`.

Lookups with no dedicated index (rely on the primary key range of the collection or the
org scope index): `principals.find_by_field("user_id")` (three call sites),
`list_by_field("user_id")` for principals, `audit_events` filters other than the first
keyset columns, `evaluation_*` and `tool_invocations` reads, `skill_*` reads by `skill_id`
(only the unique index with `org_id` first), `role_bindings` by principal (unique index
starts with `org_id, principal_id` and so serves it).

Connection pool: `open_postgres_pool(max_size=10)` per process by default
(`ProductPostgresStore(max_size=10)`); each worker loop and each API request borrows a
connection per transaction.

## 8. Questions for the ER and schema review

Modelling
- [ ] Should the product keep one JSONB table, or move the high-volume, high-integrity
      collections (`audit_events`, `task_*`, `*_runs`, `execution_jobs`, `memberships`,
      `account_*`) to typed tables with foreign keys, keeping JSONB for the flexible
      documents (`workflow_versions`, `capability_manifest`)?
- [ ] Which references must be foreign keys (every `org_id`; `workspace_id` to
      `workspaces`; `memberships` to `accounts`, `users`, `principals`; versions to
      their parents; `task_*` to `tasks`)? What `ON DELETE` rule should replace the
      hand-written `PURGE_ORDER`?
- [ ] Is the global primary key `(collection, record_id)` acceptable, or should it be
      `(org_id, collection, record_id)` and drop the `org:id` key workaround and global
      id namespace (while accounts and descriptors remain global)?
- [ ] Is the account / tenant split right: `memberships` carries `org_id` while its
      account is global and is read under system scope `account`. Should accounts live
      in a separate schema or database role?
- [ ] Should `Organization.status` and `OrganizationLifecycle.state` be one record
      (they exist as two only for older readers)?
- [ ] Should audit events, usage and billing records be append-only with database
      protection (revoke `UPDATE` and `DELETE`, or trigger)?
- [ ] Where do quotas, usage metering, invoices and payment-gateway events (phases 5
      and 7) belong: per-organization collections, a new global billing schema, or
      account-level (an account can own several organizations)?
- [ ] Is one `Account` to many `Organization` the right billing root? The quota
      ledger already counts per account (`account_organization_requests`).
- [ ] Enterprise identity (phase 8): should `sso_*` be revived as collections with
      their original constraints, with domain claims, SCIM provisioning and
      organization-level identity provider settings modelled as typed tables?

Integrity and consistency
- [ ] Add `CHECK` constraints on the scope columns (for example non-null `org_id` for
      tenant collections) and a trigger or generated column equating them with the payload?
- [ ] Should `workspace_id` membership in an organization be enforced beyond
      `plugin_installations`?
- [ ] Which of the missing uniqueness rules in section 5.1 item 10 are business rules?
- [ ] Is advisory-lock serialisation of org administration sufficient, or should
      invariants be moved into constraints?
- [ ] Should RLS be forced for the owner and always on (require `WEAVES_DB_APP_ROLE`
      in production), with a separate migration role? What is the review stance on
      the policy's `org_id IS NULL` pass-through and on `bootstrap` allowing all
      collections?
- [ ] Is it intended that the application role holds `DELETE` and `UPDATE` on
      `product_schema_migrations` and on `product_plugin_ownership*`?

Physical design
- [ ] Replace text comparisons of timestamps inside JSON with real timestamp columns or
      expression indexes with casts?
- [ ] Partition `product_records` by `collection` (list) or by `org_id` (hash)? Which
      collections are hot and which are append-only?
- [ ] Review each index in physical-schema.md against actual query plans at realistic
      volume; remove those listed as unused; add missing indexes for
      `principals.user_id`, `audit_events` filters, and run/invocation lookups.
- [ ] Replace the unbounded `.list()` scans in the worker (section 7) with indexed,
      bounded queries; add retention for audit, task and run history.
- [ ] `updated_at` double clock (section 1.6 item 6): use one source.
- [ ] `jsonb` versus `json`, TOAST and `payload` size limits for `task_messages`
      (32,000 chars), `audit_events.metadata`, `workflow_versions`.
- [ ] Autovacuum settings for a table with frequent heartbeat updates
      (`task_turns`, `execution_jobs`) mixed with append-only data.

Operations
- [ ] Backup and restore, point-in-time recovery, and per-tenant restore. How does
      purge interact with backups (deleted organizations remain in backups)?
- [ ] Migration strategy: version 20 is operator-activated and absent on fresh databases;
      versions are not contiguous; `_READ_COMPATIBLE_MIGRATIONS = {19, 20, 21}`. Should
      the runner become expand/contract with explicit gates?
- [ ] Data residency, encryption at rest and key rotation: `product_secrets.key_id` is
      the first 12 hex characters of SHA-256 of the key; is there a rotation procedure?
- [ ] GDPR-style erasure: there is no account deletion or account erasure flow; the
      ownership registry mentions "audited account erasure" but no code implements it.
- [ ] Reporting and analytics access: should there be read replicas or a denormalised
      reporting schema instead of queries over `payload`?

## Tombstones (Phase 6.2)

Not in the database by design. `modules/recovery/tombstones.py` appends hash-chained JSON lines
(`tombstones.jsonl` in `WEAVES_TOMBSTONE_DIR`) for organization purge, API credential revocation and
secret deletion/rotation; `restore_reconcile.py` replays them after a restore. No migration was
needed (29 stays unused by this slice). See `infra/gcp/singlevm/README.md`.
