# Phase 5 design: limits, fair execution, usage ledger, bounded queries

Status: proposal, 2026-11-24, against `main` at 116fe15 (schema head **24**; `docs/HANDOFF.md` still
says 22/23, it is stale). Plan source: `MULTI_TENANCY_IMPLEMENTATION_PLAN.md` "Phase 5" and the shared
testing matrix. Nothing here is implemented. Numbers in section 3 are assumptions until slice 5.6
replaces them with measurements. Production is one `e2-medium` VM (2 shared vCPU, 4 GB) running
PostgreSQL, API, one worker and Caddy (`infra/gcp/singlevm/compose.yaml`, README line 7).

## 1. Current state (evidence)

### 1.1 Enqueue and claim
Work reaches the queue only through four producers (there is no HTTP route for execution jobs or
synchronous runs any more):

| Producer | Creates | Code |
|---|---|---|
| Task create / new message / retry / requeue | `task_turns` (queued) | `modules/tasks/service.py:292,399,492`, `modules/tasks/lifecycle.py:104`, `modules/tasks/worker_service.py:230` |
| Schedules, webhooks/triggers | `execution_jobs` via `enqueue_agent_run` | `modules/executions/runtime_queue.py:32` (called from `modules/workflows/runtime_schedules.py:376`, `runtime_triggers.py:348`) |
| Evaluation runs | `execution_jobs` | `modules/evaluations/runtime_queue_operations.py:151` |
| Job retry | `execution_jobs` | `modules/executions/runtime_job_lifecycle.py:339` |

Canvas (studio) runs create ordinary tasks (`modules/workflows/studio_execution.py:359-399`), so
they are task turns too. Idempotency is a per-principal key plus **in-process `RLock` dictionaries
that are never evicted** (`modules/executions/runtime_queue.py:55-62`, `modules/agents/runtime_run_entrypoint.py:93`,
`modules/workflows/runtime_execution_entrypoint.py:84`, `modules/evaluations/runtime_queue_operations.py:56`): unbounded memory growth, and
not a cross-process guard (the DB unique indexes are).

Claim SQL, identical for both queues (`infrastructure/persistence/postgres_query_repository.py:227-275`):

```sql
WITH candidate AS (
  SELECT record_id FROM product_records
  WHERE collection = %s [AND org_id = %s] [AND workspace_id = %s]
    AND payload->>'status' = 'queued'
  ORDER BY created_at, record_id
  LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE product_records AS job SET payload = job.payload || jsonb_build_object(
  'status','running','attempts',COALESCE((job.payload->>'attempts')::int,0)+1,
  'worker_id',%s,'started_at',...,'updated_at',...), updated_at = %s
FROM candidate WHERE job.collection = %s AND job.record_id = candidate.record_id
RETURNING job.payload
```

Both callers pass `org_id=None, workspace_id=None` under `system_scope("queue")`
(`modules/executions/runtime_worker.py:29`, `modules/tasks/worker_service.py:60`), so the claim is
global oldest-first. Supporting indexes are partial on `queued` only: `product_records_execution_job_claim_idx`
(`infrastructure/database/store.py:87`) and `product_records_task_turn_claim_idx` (`:207`), both
`(created_at, record_id)` with no `org_id`. There is no running-count index.

### 1.2 Worker concurrency
`apps/worker/main.py:107-157` is one single-threaded loop: dispatch schedules (5 s), studio tick,
claim one job, then claim one turn, run each to completion on the loop thread, sleep when idle
(prod poll 3 s). Consequences:
- Global concurrency is **1 execution per worker process**; the compose file runs one worker.
  A tenant with a backlog holds the only slot for as long as it has older rows.
- Recovery (30 s), schedule dispatch, lifecycle purge and the studio tick all run **between**
  executions, so one long call (agent runtime budget up to 3600 s, `contracts/v1/agents.py:41`)
  delays them. Lease heartbeats are separate threads (`runtime_worker.py:38-46`) so leases survive.
- DB pool is 10 connections per process (`infrastructure/database/pool.py:12`).

### 1.3 Limits that exist today
Only: per-account organization creation cap (`WEAVES_ACCOUNT_ORGANIZATION_LIMIT`,
`modules/auth/account_organizations.py:92,302`), Postgres-backed sliding-window `LoginRateLimiter`
used by org creation, slug checks and the public waitlist (`application/ports/throttling.py`,
`product_login_throttle`), per-run budgets (`AgentBudget`: runtime, tool calls <=100, output
tokens, optional `max_run_cost_usd`), request body caps on the waitlist route, an org export byte
cap. **There is no per-organization request rate limit, seat, schedule, queue-depth, running-work,
tool-call-per-day, storage or usage limit, and no global emergency limit.** Admission of a new task or job never
checks the number already queued.

### 1.4 Model calls and usage data
- Single call site: `self.model_gateway.complete(...)` at `modules/agents/runtime_run_model.py:67`
  (one provider call per agent run; task turns, execution jobs and evaluation cases all reach it
  through `run_agent`, `modules/agents/runtime_run_entrypoint.py:45`).
- Providers return tokens and the adapters normalize them into `ModelUsage(input_tokens,
  output_tokens, total_tokens)` (`contracts/v1/model_execution.py:26-36`; Anthropic
  `clients/model_providers/anthropic.py:125-145`, OpenAI-compatible `openai_compatible.py:149-178`, Gemini `gemini.py:127-146`).
  Cached/reasoning token breakdowns are discarded.
- Stored: `AgentRun.input_tokens/output_tokens/estimated_cost_usd` (`contracts/v1/execution_records.py:206-210`),
  written on success (`modules/agents/runtime_run_lifecycle.py:354-356`). Cost exists only when the
  model profile has both per-million rates (`runtime_run_targets.py:221-241`), otherwise `NULL`.
  On a failure *after* the provider answered (schema invalid, cost cap exceeded) usage goes only
  into the audit event metadata (`runtime_run_model.py:226-233`), not the run row.
- **Not stored:** anything on provider errors/timeouts (unknown outcome), anything per organization,
  per agent or per day, any pre-call reservation. A crash between the provider response and the run
  write loses the tokens entirely. No usage/cost endpoint exists; "AI usage and cost per
  organization" cannot be answered without scanning every `agent_runs` row of the tenant.
- Keys are customer-supplied (BYOK) via tenant-owned encrypted secrets, so this is metering and
  capacity control, not charging.

### 1.5 Unbounded queries
Full-collection scans in system scope (`docs/DATA_MODEL.md` section 7):
- `modules/executions/recovery_service.py:75` (`runs.list()`) and `:183` (`execution_jobs.list()`) every 30 s;
  `modules/governance/action_recovery_service.py:52` (`tool_invocations.list()`) every 30 s.
- `modules/workflows/studio_scheduling.py:23-24` (`settings.list()`, `runs.list()`) **every loop iteration**.
- `modules/workflows/runtime_schedules.py:189`, `runtime_triggers.py:151` list all of an org's schedules/triggers per dispatch.
- Start-up/other: `modules/auth/bootstrap.py:156`, `modules/models/bootstrap.py:74,90`,
  `modules/integrations/oauth_state_service.py:130`, `application/descriptor_catalog.py:21,38`.

Org/workspace-wide `list_scoped` whole-collection loads filtered in Python:
- `modules/tasks/execution_summaries.py:57,70` (all turns and all agent runs, for every
  `GET /tasks?include_execution=true`), `modules/tasks/worker_service.py:357`.
- `modules/agents/runtime_run_entrypoint.py:159,173,178,208`, `modules/evaluations/runtime_reports.py:122-133`,
  `modules/executions/recovery_service.py:113,142`, `modules/auth/organization_guard.py:150` (all audit events
  to find `organization.created`), `modules/workflows/studio_context.py:152`.

HTTP endpoints that return a whole collection or load it all and slice afterwards:
- No limit at all: `GET /users` (`modules/auth/runtime_users.py:242`), `/agents` (`runtime_catalog.py:56`),
  `/workflows` (`definition_service.py:81`), `/workflow-triggers`, `/workflow-schedules`, `/skills`
  (`modules/skills/service.py:67`), `/plugin-installations`, `/api-credentials`, `/model-profiles`,
  `/model-providers`, `/roles`, `/role-bindings`, `/workspaces`, `/evaluation-suites`.
- `limit` present but applied after a full load: `GET /approvals` (`modules/governance/http/routes.py:131`, plus
  `routes.py:55` rescans all tool invocations **per approval**, N x M), `/evaluation-runs`
  (`runtime_queue_operations.py:203`), `/evaluation-reports` (`runtime_reports.py:280`),
  `/workflows/{id}/runs` (`studio_context.py:152`, then `[:100]`).
- Properly keyset-bounded: `GET /tasks`, `/tasks/{id}/messages`, `/audit-events` (+ `/export`).
- All-user email scan: `_ensure_user_email_available` (`modules/auth/runtime_users.py:136-146`) loads every
  user of the org although a unique `(org_id, lower(email))` index already exists (`store.py:115-122`).
  Sign-in linking is already indexed (`user_email_lower_idx`, `account_schema.py:72`).
- Growth with no retention: `audit_events`, `task_messages`, `task_turns`, `agent_runs`, `tool_invocations`,
  `artifacts` (`docs/DATA_MODEL.md` section 7).

### 1.6 Home metrics
Computed in the browser. On every visit `weaves-app/dashboard.js:337,354-370` fetches
`/tasks?limit=100&include_execution=true` and then **follows `next_cursor` through the entire task
history** before rendering; polling (`dashboard.js:1840-1850`, every 1.5 s while anything is
queued/running) refetches the first page with `include_execution=true`, which triggers the two
full scans in 1.5. Tile counts are computed client side from that array (`dashboard.js:783,800,967`).

### 1.7 Logging and correlation
API: `X-Request-Id` is a fresh ID per request (`apps/api/error_mapping.py:16-20`, set in the auth
middleware `apps/api/authentication.py:315-322`), returned in the header and in 500 bodies, but
not bound to a context variable and not logged on normal requests; the API never calls
`basicConfig`. Worker: plain text format (`apps/worker/main.py:17-21`) with ids inlined in messages
(`job_id=`, `turn_id=`). Org/workspace/principal live in a ContextVar (`shared/request_context.py`)
but are not attached to log records. No request id or job id is carried from API to job to run
(audit `request_id` is sometimes set to the target id, e.g. `runtime_queue_operations.py:180`). There is no
metrics endpoint, no queue-age or per-tenant measurement anywhere.

## 2. Slice plan

Common rules: forward-only numbered migrations in their own module (pattern `waitlist_schema.py`),
registered by appending one tuple in `infrastructure/database/store.py:347-350`; contracts in
`contracts/v1` with `extra=forbid`; new org-scoped data gets an `ownership.json` entry, an org
purge/export decision (`modules/auth/organization_retention.py`) and RLS; each slice ends with
unit, PostgreSQL (restricted `weaves_app` role), real-stack controls and a budget-gated live run
as AGENTS.md requires. **Migration numbers are reserved up front** (5.1=25, 5.2=26, 5.3=27, 5.4=28)
so parallel slices never collide; the runner applies any missing version regardless of order
(`store.py:427-436`), but every migration is additive, so order is also safe.

### 5.0 Prelude (sequential, tiny, first)
Goal: remove the shared-file hotspots so 5.1 to 5.5 can proceed in parallel.
- `shared/limits_config.py`: frozen dataclass of every setting in section 3 read from env, with
  `WEAVES_LIMITS_MODE=off|observe|enforce` (default `observe`). `application/ports/limits.py`:
  `LimitPolicy` Protocol (`for_org(org_id) -> OrgLimits`) with the static env-backed implementation.
- Split the worker loop into `apps/worker/loop.py` with a tick list (`apps/worker/ticks.py`), so
  slices add one registration line instead of editing `main.py`.
- Add `tenancy_harness/phase5/` package skeleton (empty modules, required-control ids T5-* in `pair.json`
  declared as `planned`, not required until their slice lands).
- Migration: none. Frontend: none. Tests: unit for config parsing and bounds.

### 5.1 Tenant limits and atomic admission
Goal: enforce per-org limits at every admission point; atomic across API and worker; global emergency brake.
- New: `modules/limits/` (`service.py` admission API, `errors.py`), `contracts/v1/limits.py`
  (`OrganizationLimits`, `LimitUsageView`), `apps/api/request_limits.py` (per-org/per-principal request
  token bucket middleware, registered from `apps/api/app.py`), `apps/api/limit_errors.py` (HTTP 429 mapping,
  code `limit_exceeded`, `Retry-After`, `{resource, limit, used}` without other-tenant data).
- Hook (small edits): `modules/tasks/service.py` (create_task, append message), `modules/executions/runtime_queue.py`,
  `runtime_job_lifecycle.py` retry, `modules/evaluations/runtime_queue_operations.py`,
  `modules/workflows/runtime_schedules.py` (schedule creation count), `modules/auth/user_administration_service.py` / `runtime_users.py`
  (seats), `modules/governance` tool execution (daily tool-call count).
- Mechanism: inside the same transaction that inserts the row, take
  `acquire_advisory_transaction_lock("limit:{org}:{resource}")` (existing, `store.py:488`), count from the source of truth
  with a partial index, insert only if below the cap. No separate counters, so a crash cannot leak capacity. Cancelled/failed/terminal
  rows stop counting automatically. Idempotent replays return the existing row before admission is consulted (a replay must not consume or fail on a full queue).
- Overrides: `organization_limits` collection (migration 25: one row per org, operator-written only
  by a new script `infra/gcp/singlevm/organization_limits.py`, mirroring `organization_lifecycle.py`) plus
  count indexes on `product_records` (`(org_id) WHERE collection IN (queued/running work)`; schedules; users).
  Global emergency: `WEAVES_ADMISSIONS_PAUSED` and global queued cap, checked without a lock (approximate by design).
- API: `GET /api/v0/organization/limits` (usage vs limit for Owner/Admin, read-only); every refused
  admission is `429` with the code above (a new contract, no compatibility shim).
- Frontend: handle `limit_exceeded` in the `api()` wrapper (`dashboard.js`) and show the shared notice with
  retry-after; Home composer disables with a reason; small read-only "Limits" card in organization settings.
- Tests: unit (arithmetic, replays before admission); PostgreSQL: N threads x M processes racing at limit-1 never exceed the cap,
  same idempotency key in two tenants, cancellation frees capacity, restricted role cannot read another org's limits;
  real stack `T5-I01` (API refuses the (cap+1)th task while the worker is running; org B unaffected; durable row and model-call counts
  unchanged on refusal); live run: one real call succeeds under limits, a refused task makes zero provider calls (relay count asserts it).
- Depends on 5.0. Rollout: observe mode first (log would-be refusals), then enforce.

### 5.2 Fair claiming and per-tenant concurrency
Goal: a hot tenant cannot starve others; bounded concurrent executions per org; worker runs N slots.
- Files: `infrastructure/persistence/postgres_query_repository.py` (new fair claim), `memory_repository.py` and
  `application/ports/repository.py` (same semantics in memory), `modules/executions/runtime_worker.py`,
  `modules/tasks/worker_service.py`, new `apps/worker/executor.py` (bounded slot pool), tick registration only in `ticks.py`,
  `infrastructure/database/scheduler_schema.py` (migration 26).
- Claim algorithm (one statement family, both collections counted together): pick candidate orgs that have queued work
  and fewer than `max_running_per_org` running rows (derived by counting `running`/`cancel_requested` rows, never a stored counter, so
  crashes cannot drift it), order them by `tenant_scheduler_state.last_claimed_at NULLS FIRST` (round robin), take the oldest queued
  row of the first org under `pg_try_advisory_xact_lock(org)` + `FOR UPDATE SKIP LOCKED`, upsert `last_claimed_at` in the same transaction.
  Enumerating orgs with queued work is bounded by the 5.1 queue cap times org count. Within an org the order stays oldest-first.
- Worker: `WEAVES_WORKER_SLOTS` threads each running claim -> execute with its own tenant context; a supervisor thread keeps
  recovery, dispatch, studio tick and purges running independent of executions (fixes 1.2). Pool size raised to `slots*2 + 4`.
  Also bound queue age (log + metric when oldest queued exceeds `queue_age_warn_seconds`) and make idempotency lock dictionaries bounded (LRU with cap).
- Migration 26: `tenant_scheduler_state(org_id pk, last_claimed_at)` with RLS (system purpose `queue` only), partial indexes
  `(org_id, created_at, record_id) WHERE queued` and `(org_id) WHERE running` for both collections.
- Contracts/API/frontend: none required; optional later: "waiting for capacity" state text on the task (not in this slice).
- Tests: unit (fairness order, cap, memory parity); PostgreSQL: concurrent claimers never exceed the cap, skip-locked
  parity, crash of a claimer (kill mid-run) frees the slot after lease recovery, a suspended org's rows are still not executed;
  real stack `T5-I02` with two native workers: A has 100 queued slow-fake-model tasks, B submits one and starts within a declared
  bound; live run: unchanged single-call smoke through the multi-slot worker.
- Depends on 5.0 only (reads caps through `LimitPolicy`). Highest behavioural risk (section 4).

### 5.3 Usage ledger and idempotent reservations
Goal: per organization/agent/run token and estimated-cost records that survive crashes, plus pre-call reservation against usage limits.
- New: `modules/usage/` (`service.py` reserve/settle/release, `reconciler.py`, `queries.py` for rollups), `contracts/v1/usage.py`
  (`UsageEvent`, `UsageSummary`, `UsageReservation`), `infrastructure/database/usage_schema.py` (migration 27).
- Edits (narrow): `modules/agents/runtime_run_model.py` (reserve before `complete()` at line 67, settle after, release/mark unknown
  on exceptions), `modules/agents/runtime_run_targets.py` (reuse the cost calculation, move it into `modules/usage`), the
  `ModelUsage` contract gains optional `cached_input_tokens` / `reasoning_tokens` only if providers return them
  (`contracts/v1/model_execution.py`; adapter edits are minimal). Do **not** touch `recovery_service.py` (5.4 owns it): the reconciler
  is its own tick.
- Data model (dedicated relational table, deliberate exception to the one-JSONB-table rule, flagged for the architect review: append-heavy,
  needs SUM/GROUP BY by org/agent/day, needs a real unique key):
  `usage_ledger(org_id, workspace_id, reservation_key, agent_run_id, agent_id, provider_id, model_id, state, reserved_input, reserved_output,
  input_tokens, output_tokens, estimated_cost_nanousd, reserved_at, settled_at, ...)` with unique `(org_id, reservation_key)`,
  `reservation_key = agent_run_id` (one call per run, which is already deduplicated by `product_records_run_idempotency_uq`), RLS on `org_id`,
  and a `usage_daily` rollup (org, day, agent, model) maintained transactionally on settlement. Money as integer nano-USD, never float
  (same rule as Phase 7 and `tenancy_harness/budget.py`).
- States: `reserved -> settled` (provider usage known), `reserved -> released` (no provider request was made), `reserved -> unknown`
  (request may have reached the provider: timeout, connection loss, crash). `unknown` keeps the reservation amount as the charged upper bound
  and is never double counted. Reconciler: a reservation whose owning job/turn lease expired (use the existing lease and recovery
  fence, not a wall-clock TTL, because runtime budgets reach 3600 s) is moved to `unknown`, or `settled` when the run row already has tokens.
  Settlement is an upsert on the unique key, so replays and a retried settle are no-ops.
- Failure cases covered: schema-invalid or over-budget output after a successful provider answer is still `settled` (usage today is only in audit
  metadata, 1.4); provider 4xx before processing is `released`.
- Admission: reserve = estimated input (chars/3 of the built prompt) + `max_output_tokens`; refused with `limit_exceeded` when the org's day/month
  token or estimated-cost cap would be exceeded by settled + outstanding reservations (advisory lock on `usage:{org}`).
- API: `GET /api/v0/usage?from=&to=&group_by=agent|model|day` (Owner/Admin; bounded range, max 92 days), included in tenant export
  by rule, purged with the org except monthly rollups that the owner may elect to retain (decision for the owner, section 4).
  Operator report script `infra/gcp/singlevm/usage_report.py` for the business need "AI usage and cost per organization".
- Frontend: new read-only Usage view (organization settings) with day totals and per-agent table; no editing.
- Tests: unit (state machine, arithmetic in integers, replay); PostgreSQL: concurrent reservations at the cap never exceed it, kill-restart between
  provider response and settle leaves `unknown` then reconciles with no double count, cross-tenant key reuse is independent, RLS denial;
  real stack `T5-I03` (fake model reports tokens; ledger equals run rows; crash injection with a worker kill during reservation); live run:
  the relay's recorded usage equals the ledger row for the real call (this is the only paid check of the slice, ~1 cent).
- Depends on 5.0 (policy Protocol) and merges cleanly with 5.1 (admission errors reuse its 429 mapping: whichever merges first owns `apps/api/limit_errors.py`;
  the other imports it). Needs 5.5's correlation fields only for log polish, not correctness.

### 5.4 Bounded queries and server-side Home metrics
Goal: remove every unbounded scan and history traversal listed in 1.5/1.6.
- Recovery and dispatch: replace `.list()` in `recovery_service.py:75,183`, `action_recovery_service.py:52`, `studio_scheduling.py:23-24`,
  `runtime_schedules.py:189`, `runtime_triggers.py:151` with `list_status_updated_before` (exists, `postgres_query_repository.py:208`) / due-time
  queries plus a persisted checkpoint (`recovery_checkpoints`, bounded batches of 200). Add partial indexes (migration 28) for
  `workflow_runs`/`execution_jobs`/`tool_invocations` `running` by `updated_at`, `canvas_settings` by `next_run_at`, `agent_runs` by `run_id`/`task_id`,
  `approval_requests` by `status`, `tool_invocations` by `approval_request_id`.
- Email scan: `_ensure_user_email_available` becomes an indexed point lookup on the existing unique index (`runtime_users.py:136-146`).
- `execution_summaries.py:57,70` and `worker_service.py:357`: query by `task_id`/`run_id` (new indexes) instead of `list_scoped`; `organization_guard.py:150`
  reads one audit row by action+target via index.
- List endpoints: every collection endpoint in 1.5 gets `limit` (default 50, max 100) and keyset `cursor`, returning `{items, next_cursor}`.
  This is a breaking response change (bare arrays today); internal mode forbids compat code, so backend and frontend ship as a coordinated pair
  and the harness pin moves with them. Server-side filters replace client-side ones for approvals (`status`, workflow run lists).
- Overview: new `modules/overview/service.py` and `GET /api/v0/overview` (counts by task status, pending approvals, active/queued runs, last-7-day
  run and failure counts, first-run checklist booleans) computed with `COUNT ... FILTER` over indexed partial scans, scoped to the caller's visibility
  (private tasks excluded unless owned), short per-process cache (5 s). Batched workflow summaries: `GET /api/v0/workflows?include=summary`.
- Frontend (separate repo, same slice): `dashboard.js:337,354-370,1840-1850` call the overview endpoint and keep one page of recent tasks; polling hits
  the overview only; list screens page with `cursor`; no history traversal.
- Tests: unit (cursor encode/decode, visibility); PostgreSQL: seed 20k tasks/200k turns across 50 orgs, assert `EXPLAIN` plans use indexes and the
  per-request SQL count and row count are constant, recovery tick cost does not grow with tenant count; real stack `T5-I04` (browser Home with a seeded
  large history makes a bounded number of requests, tile numbers equal DB truth, cross-org counts never leak); live run: Home after one real run shows it.
- Depends on 5.0. Touches many modules but only edits listing/recovery code; **overlaps** 5.1 in `modules/auth/runtime_users.py` (disjoint functions; trivial
  merge) and avoids `runtime_run_model.py`, 5.2 files and 5.3 files.

### 5.5 Structured correlation and metrics
Goal: tenant/request/job correlation in logs and low-cardinality metrics, before the load tests.
- New: `shared/correlation.py` (ContextVars: request_id, org_id, workspace_id, principal digest, job_id, turn_id, run_id), `shared/logging_setup.py`
  (JSON formatter that injects those fields, redaction of tokens/secret-like keys/prompt text, used by API and worker), `apps/api/metrics.py`
  (`/api/v0/internal/metrics`, Prometheus text, reachable only on the Docker network, not routed by Caddy), `shared/metrics.py` (in-process counters/histograms).
- Edits (narrow): `apps/api/authentication.py` and `error_mapping.py` (request id bound to the context; accept an inbound `X-Request-Id` only
  when well-formed, else generate), `apps/worker/loop.py` (bind job context per claim), `modules/executions/runtime_worker.py` (log lines already carry job_id;
  now structured), propagation: persist `request_id` on the created task/job/turn so worker logs link back (`contracts/v1`: optional `request_id` field on `ExecutionJob`/`TaskTurn`;
  no migration, JSON payload).
- Metrics: `queue_depth{queue}`, `oldest_queued_age_seconds{queue}`, `running{queue}`, `admission_refused_total{resource}`, `model_call_seconds`/`_total{outcome}`,
  `usage_tokens_total{direction}`, `http_request_seconds{route_template,status_class}`, `db_pool_in_use`. **Org id is a log field only, never a metric label**;
  per-tenant numbers come from SQL (`usage_report`, `queue_report`) to control cardinality.
- Frontend: none (the existing request id in error banners now matches log lines).
- Tests: unit (formatter redaction, no secret/prompt in any field, context isolation across threads); PostgreSQL: none new; real stack `T5-I05` (a request id
  from the browser appears on the API line, the job line and the run's audit trail, never in another tenant's rows); live run: one real call, assert the log
  stream contains no key and no prompt text (grep the container logs for the fixture key and prompt).
- Depends on 5.0. Edits `authentication.py`/`error_mapping.py` which no other slice touches (5.1 uses its own files).

### 5.6 Noisy-neighbor and load tests, capacity envelope (sequential, last)
Goal: measured limits and the written beta capacity envelope (exit gate).
- New: `tenancy_harness/phase5/` (`load.py`, `noisy_neighbor.py`, `fake_slow_model.py` with configurable latency/tokens, `reservation_recovery.py`,
  `large_history_seed.py`), `docs/phase-5-capacity-envelope.md` (results, hardware, declared B-tenant envelope), `scripts/phase5_load.py`.
- Scenarios: (1) saturate A (backlog + slow model) while B submits normal work: record per-tenant queue delay, completion latency, throughput, errors, pool
  use, RSS; B must meet the envelope declared **before** the run. (2) Thousands of records and large histories: request counts and plans stay bounded.
  (3) Restart API and worker during reservation, execution, settlement: ledger reconciles, no duplicate external effect (fake connector call counts). (4) Tenant count
  growth: recovery/poll cost flat from 10 to 500 orgs.
- Hardware: the real `e2-medium` shape in compose (limits set to match), not a laptop; fake provider for volume, **one** budget-gated live run (<= 5 calls) to
  calibrate real latency/tokens per call. Results replace the section 3 assumptions via env defaults in `shared/limits_config.py`.
- Migration: none. Frontend: none except vendoring the harness (`scripts/sync_tenancy_harness.py`, new frontend PR, re-pin in `pair.json`).
- Depends on 5.1 to 5.5 all merged and deployed.

### 2.1 Dependencies and parallelism

```
5.0 --> {5.1, 5.2, 5.3, 5.4, 5.5} --> 5.6
```
| Slice | Migration | Parallel with | Shared files to coordinate |
|---|---|---|---|
| 5.0 | no | none (first) | creates the shared seams |
| 5.1 | 25 | 5.2, 5.3, 5.4, 5.5 | `apps/api/limit_errors.py` (with 5.3), `runtime_users.py` (with 5.4) |
| 5.2 | 26 | 5.1, 5.3, 5.4, 5.5 | none beyond registries |
| 5.3 | 27 | 5.1, 5.2, 5.4, 5.5 | `limit_errors.py` (with 5.1) |
| 5.4 | 28 | 5.1, 5.2, 5.3, 5.5 | `runtime_users.py` (with 5.1); frontend `dashboard.js` is its only heavy frontend edit |
| 5.5 | no | 5.1 to 5.4 | `apps/worker/loop.py` binding line (with 5.2) |
| 5.6 | no | none | needs all |

Disjoint source files, safe for separate agents: 5.1 (`modules/limits`, `apps/api/request_limits.py`), 5.2 (repository claim code, `apps/worker/executor.py`),
5.3 (`modules/usage`, `runtime_run_model.py`), 5.4 (recovery and listing code, `modules/overview`), 5.5 (`shared/correlation.py`, logging, metrics).
Unavoidable one-line conflicts, resolved by the integrator: the migration tuple in `store.py`, `apps/worker/ticks.py`, `tenancy_harness/pair.json` and
`bundle.lock.json` (regenerate with `scripts/sync_tenancy_harness.py` after the last merge, never hand-merge), `docs/DATA_MODEL.md` (regenerate with
`scripts/export_data_model.py`). **Merge order recommendation:** 5.0, 5.5 (so the other slices' real-stack failures are diagnosable), then 5.1, 5.2, 5.3,
5.4 in any order, one deploy at a time; every slice that edits the harness needs its own frontend vendoring PR, which serializes merges (frontend `main` deploys only if its real-stack job passes).
5.4 is the only slice with a hard cross-repo response-shape change; ship backend and frontend PRs together.

## 3. Proposed default limits (all `WEAVES_*` env, per organization unless noted)

Every value is an ASSUMPTION to be replaced by slice 5.6 measurements. Reasoning is sized for one `e2-medium` shared with Postgres and four slots.

| Setting | Default | Reasoning (ASSUMPTION) |
|---|---|---|
| `WEAVES_LIMITS_MODE` | `observe` | Ship measuring first; flip to `enforce` per environment. |
| `WEAVES_WORKER_SLOTS` | 4 (global) | Model calls are network bound; 2 vCPU shared with Postgres cannot do more CPU-heavy work. |
| `WEAVES_LIMIT_MAX_RUNNING_PER_ORG` | 2 | Half of the slots, so one org never takes the whole worker. |
| `WEAVES_LIMIT_MAX_QUEUED_PER_ORG` | 50 | Bounds claim enumeration and drain time at 2 slots (~minutes). |
| `WEAVES_LIMIT_GLOBAL_MAX_QUEUED` | 500 | Emergency brake: refuse admission above this and log. |
| `WEAVES_ADMISSIONS_PAUSED` | `false` | Operator kill switch for new work; reads and cancellation keep working. |
| `WEAVES_LIMIT_REQUESTS_PER_MINUTE` | 600 per org, 240 per principal | Home polls ~40/min per open tab (1.5 s); leave 10x headroom. Token bucket, burst 100. |
| `WEAVES_LIMIT_SEATS` | 25 active users | Beta teams are small; counts non-deleted users. |
| `WEAVES_LIMIT_SCHEDULES` | 20 active | Each schedule can enqueue every few minutes. |
| `WEAVES_LIMIT_AGENTS` / `_WORKFLOWS` | 50 / 50 | Bounds catalog list cost (also bounds 5.4 page walks). |
| `WEAVES_LIMIT_TOOL_CALLS_PER_DAY` | 2,000 | Per-run cap is <=100; ~20 full runs of tool calls per day per org. |
| `WEAVES_LIMIT_TOKENS_PER_DAY` | 2,000,000 | About 400 runs at 5k tokens; BYOK so this is capacity control. |
| `WEAVES_LIMIT_TOKENS_PER_MONTH` | 40,000,000 | 20 days at the daily cap. |
| `WEAVES_LIMIT_EST_COST_USD_PER_DAY` | 25 (only when rates configured) | Stops a runaway loop on a priced profile; `NULL` rates are skipped. |
| `WEAVES_LIMIT_STORAGE_TASKS` | 10,000 open+archived tasks | Proxy for storage until a byte rollup exists (messages are capped at 32k chars). |
| `WEAVES_USAGE_RESERVATION_INPUT_CHARS_PER_TOKEN` | 3 | Conservative estimate; settlement corrects it. |
| `WEAVES_QUEUE_AGE_WARN_SECONDS` | 120 | Beta service envelope starting point for B-tenant queue delay. |
| `WEAVES_PAGE_DEFAULT` / `_MAX` | 50 / 100 | Matches existing task/audit pages. |
| `WEAVES_RECOVERY_BATCH` | 200 | Matches the existing account-purge batch size. |
| `WEAVES_USAGE_RETENTION_DAYS` | 400 | Covers a year of billing reconciliation plus margin. |
| DB pool per process | `slots*2+4` = 12 worker, 10 API | Executor threads plus heartbeats plus supervisor; Postgres default 100 connections is ample. |

## 4. Risks and rollout

Cross-cutting: every new setting defaults to `observe` (log would-be refusals, change nothing) for one deploy, then `enforce` in a second deploy after reading the log.
Deploy discipline from memory: merge, let GitHub deploy, verify (`gh run list`, health, migration recorded, no errors) before the next push; one slice per deploy.
Migrations are forward-only; images older than the new head cannot be rolled back (already true since 22), so each slice's rollback is the config flag
(`WEAVES_LIMITS_MODE=off`, `WEAVES_WORKER_SLOTS=1`, `WEAVES_FAIR_CLAIM=off`), not an image downgrade.

| Slice | Risk | Mitigation |
|---|---|---|
| 5.1 | Count indexes on `product_records` built inside the runner's single transaction lock the table; fine on today's data, not on beta data. | Create indexes before beta data exists (now), or add a non-transactional `CREATE INDEX CONCURRENTLY` step to the runner before Phase 6; migration rehearsed against a restored production dump (`check_backup.sh`). |
| 5.1 | A bad cap locks a tenant out; counting races on replays. | Observe mode first; replay check before admission; operator override script; `ADMISSIONS_PAUSED` only blocks new work. |
| 5.2 | The worker becomes multi-threaded: ContextVar tenant identity per thread, shared in-process dictionaries, pool exhaustion, `RLock` ordering; a wrong claim could run work under the wrong tenant or leave a slot stuck. | Each slot sets and resets identity around the whole unit (existing pattern); claim never returns rows outside the chosen org; `WORKER_SLOTS=1` and `FAIR_CLAIM=off` restore today's behaviour; kill-mid-run and lease-loss tests; run in production with 1 slot first, then 2, then 4, watching queue age. |
| 5.2 | Claim query cost grows with orgs having queued work. | Bounded by the 5.1 queue cap; plan assertions in 5.4/5.6 tests. |
| 5.3 | Reservation bug blocks all model calls or double counts; crash windows; the table duplicates information in `agent_runs`. | Ledger writes in observe mode never block a call (record only) for one deploy; reconciler compares ledger to `agent_runs` and logs drift; unknown-state test with a SIGKILL between provider response and settle. |
| 5.3 | New relational table departs from the one-table model and from RLS-on-`product_records`; deletion/export/retention gaps. | Explicit RLS policy in the migration, `organization_retention` and export entries, drift test extended; architect review item; owner decides if monthly rollups outlive organization deletion (privacy vs billing reconciliation). |
| 5.4 | Breaking list response shape and the removal of history traversal could blank the UI; new indexes slow writes slightly. | Backend and frontend PRs are paired and gated by the real-stack suite; one endpoint family per PR if the diff is large; index set kept to those proven by `EXPLAIN`. |
| 5.4 | Replacing full-scan recovery with due queries could miss a stuck row class. | Parity test: old scan vs new query on a seeded set must return the same ids; checkpoint is advisory, a full sweep still runs nightly. |
| 5.5 | Logging leaks prompts or secrets; metric cardinality explosion; inbound request id used for log forging. | Allow-list fields, redaction tests, org id never a label, request id charset/length validated. |
| 5.6 | Load tests on the production VM disturb real usage; paid calls overspend. | Run on a disposable clone of the VM shape, fake provider for volume, one live run through the budget relay (reserve before dispatch, never raise the USD 100 cap). |

Open decisions for the owner: whether usage rollups survive organization deletion; whether limits are operator-set only (this design) or also customer-visible
plan entitlements now (deferred to Phase 7, the `organization_limits` row is the seam); acceptable beta B-tenant queue delay (starting point 120 s).
