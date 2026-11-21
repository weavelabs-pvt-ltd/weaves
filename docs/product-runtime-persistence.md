# Product Runtime Persistence

## Status

The product runtime can use PostgreSQL for durable contract records. The
standalone runtime keeps its in-memory implementation so local scripts do not
need a database. Docker Compose starts the product API after its PostgreSQL
health check.

## Design

- `Repository[T]` remains the boundary used by the product runtime.
- `InMemoryRepository` is used when `DATABASE_URL` is empty.
- `PostgresRepository` stores versioned contract JSON in `product_records` and
  validates every read through the contract's strict JSON parser.
- Records are keyed by collection and the contract's declared ID field. Org
  and workspace IDs are indexed columns, and scoped reads apply those columns
  in SQL.
- The database connection pool belongs to the runtime and is closed with it.
- Startup applies ordered PostgreSQL schema migrations under a transaction-level
  advisory lock. Each migration is recorded only after its DDL and backfill
  succeed; already-applied migrations are skipped, and a database created by a
  newer backend version is rejected. Existing agent/model/connection
  configuration survives restarts.
- Model provider keys and connector tokens are kept in a separate
  `product_secrets` table as Fernet
  ciphertext. The encryption key is supplied through
  `WEAVES_SECRET_ENCRYPTION_KEY` and is never stored in PostgreSQL. API views
  omit `auth_ref` and never return the submitted key. Provider create, rotate,
  and catalog refresh, plus model-profile updates, commit their product-record
  changes with audit events in the same database transaction. If that
  transaction fails, newly written secrets are removed.
- Model profiles can store operator-entered input/output rates per million
  tokens. Completed agent runs record a decimal cost estimate when both rates
  and provider token usage are available. A model profile can also set a
  per-run estimated-cost cap. The runtime requires both rates when a cost cap
  is configured and marks an over-cap run failed while preserving usage and
  cost data. Since the provider reports token usage after completion, this
  detects overspend after the call and cannot prevent that call's charge.
  Estimates depend on configured rates and are not provider invoices.
- Agent versions carry configurable wall-clock, context-call, output-token, and
  estimated-cost budgets, plus an optional validated JSON Schema for structured
  model output. The runtime asks for that shape, validates the response before
  publishing it, and stores valid data under `structured_output`; invalid output
  fails the run while retaining provider usage and an audit event. Context calls
  above the per-step limit are skipped
  and named in the run output. Wall-clock limits are checked between external
  calls and after model completion; they cannot interrupt an in-flight provider
  or connector request. When a model response returns after the limit, the run
  fails but its reported usage and estimated cost are retained.
- Provider responses are bounded to 100,000 characters. Run output records and
  artifacts have compatible bounded storage so a valid model response is not
  rejected by the former 2,000-character artifact-summary limit.
- Model context is bounded to 32 retrieved records and 48,000 serialized
  characters; prior workflow results are bounded to 30,000 serialized
  characters. The run output records omitted and shortened source IDs. This
  keeps model requests within the provider-neutral message contract and leaves
  room in artifact provenance for up to nine prior workflow steps.
- Workspace knowledge documents are contract-validated product records and are
  searched only through the registered `knowledge.search` capability.

The generic record table is an initial persistence adapter for the current v0
contracts. If a collection needs relational constraints, high volume queries,
or database-enforced tenant isolation, it can move behind the same repository
port to a dedicated table without changing the agent runtime or API contract.

## Local use

`docker compose up --build` runs the product API at port 8001 and PostgreSQL on
port 5432. The product API uses `postgresql://...@postgres:5432/weaves` in
Compose. Its database volume is named `weaves-postgres`.

For memory-only mode, leave `DATABASE_URL` unset. The health endpoint reports
the selected storage mode and checks PostgreSQL readiness when PostgreSQL is
configured.

## Current boundaries

- An organization is provisioned by a verified account
  (`POST /api/v0/account/organizations`) or by an operator on the host; either way
  it gets a default workspace, an owner principal, a mock model profile, an
  approval policy, and a local knowledge installation. Authenticated requests
  derive organization scope from the principal behind the credential (an
  account session names its organization with `X-Organization-Id`);
  `X-Workspace-ID` can select only a workspace granted to that principal. The
  request's tenant scope is propagated through API service calls, including
  worker execution. Members are provisioned by an administrator and activated by
  their first verified account sign-in; there are no invitation tokens.
  Account organization creation is throttled per account and source address.
  Service accounts and API credentials authenticate automation:
  PostgreSQL stores only token digests, supports expiry, and revokes credentials
  immediately. `PUT /api/v0/api-credentials/{credential_id}/rotate` atomically
  revokes an active credential and issues a replacement for the same principal,
  preserving its expiry. The plaintext replacement is returned once.
  People sign in only through accounts: an emailed one-time code opens an
  account session (bearer, or an HttpOnly cookie with CSRF protection) that
  acts in one organization per request through `X-Organization-Id`.
  `GET /api/v0/auth/me` shows the resolved identity. Account sessions are stored
  as digests, expire after 12 hours, and every request re-derives the
  membership, user, principal and organization state, so suspension applies
  immediately. Sign-in attempts are throttled per address and per source address
  with hashed bucket keys; PostgreSQL shares counters across API instances and
  memory mode keeps bounded process-local counters. `Retry-After` is returned
  without account-specific error text. Reverse-proxy deployments must configure
  their ASGI server to trust only known proxies so `request.client.host` is
  accurate. MFA, SSO/OIDC, refresh tokens and account recovery remain
  unimplemented. API credentials remain active until separately revoked or
  expired. Permission checks and run/audit actors use the resolved principal.
  Without environment-configured tokens, the seeded local developer remains
  available only in memory-only local development. When PostgreSQL is
  configured, protected product routes require a bearer token even if no
  environment token is configured; health, account sign-in and the OAuth
  callbacks remain public entrypoints.
- OpenAI, Anthropic, Gemini, and OpenAI-compatible API-key providers are
  supported; provider and model-profile administration requires
  organization-level `models.manage` because those records are shared across
  workspaces. Azure OpenAI and Bedrock credential flows are deferred.
- Local knowledge and read-only GitHub issue/pull-request, Slack message, and
  Jira issue searches are working context sources. Jira comments are the only
  write action; they require an approval request and an explicit decision.
  MCP Streamable HTTP tools are also available as read-only context sources;
  only admin-selected, locally allowlisted tools can be installed.
- `POST /api/v0/runs` remains synchronous. The separate execution-job API and
  asynchronous evaluation API use PostgreSQL-backed durable records and a
  worker process. Evaluation progress is persisted by case; recovery completes
  reports only when all case results were saved, otherwise it marks the
  evaluation non-retryable to avoid silently repeating paid calls. Start
  records, each
  invocation/audit pair, terminal run/agent-run/artifact/audit records, and
  stale-run recovery are committed as short cross-record transactions.
  External model and connector calls happen between these transactions; a
  later failure does not roll back earlier successful read invocations. Queue
  jobs renew a worker-fenced lease every 20 seconds and are reconciled after
  two minutes without a heartbeat. They do not retry automatically. Callers can
  manually retry retryable failures with idempotency and lineage. See
  [product-execution-queue.md](product-execution-queue.md).
- Published workflows can also be triggered by an organization-managed
  webhook. The trigger secret is returned only at creation/rotation and only
  its SHA-256 digest is persisted. A valid event requires a stable event ID,
  carries a bounded JSON object into an idempotent execution job, and runs
  under the trigger creator's current permissions. Trigger delivery is
  PostgreSQL-only because it uses the durable worker queue. Ingress
  rate-limiting and provider-specific signature schemes remain deployment
  responsibilities; see [product-workflows-v0.md](product-workflows-v0.md).
- Workflow administrators can configure PostgreSQL-backed recurring interval
  schedules. The worker serializes each due occurrence across processes,
  enqueues it as an idempotent durable execution job, and advances the next
  time without replaying every missed interval. Schedules recheck the creator's
  current permissions and enter an inspectable error state when the workflow
  can no longer run; see [product-workflows-v0.md](product-workflows-v0.md).
- `Idempotency-Key` deduplicates requests per principal and replays successful
  runs, including after restart when PostgreSQL is used. A partial unique index
  prevents cross-process duplicate run creation, and a losing request replays
  the winner before any model or connector call.
- Run execution refreshes its persisted heartbeat before each outbound context
  or model call. Startup marks runs with no heartbeat for four hours as failed
  and emits a recovery audit event, including when interruption happened
  between writing the workflow and agent-run records. This threshold assumes
  the current synchronous connector call bounds. MCP stream calls have a
  30-second cumulative deadline and five-second I/O timeouts. Synchronous run
  requests do not yet have cancellable or independently renewed run leases.
- Conversation threads retain run task/response records and provide up to
  eight recent successful turns (bounded to 24,000 characters) to a follow-up
  run. Memory remains explicit, scope-controlled, and retrievable through the
  context builder. Memory management lists only shared organization items and
  items in the selected workspace; workspace roles cannot create or delete
  organization-wide memory. Memory creation accepts a retention class and
  optional expiry. Expired items are immediately omitted from retrieval and
  management lists, then deleted with an audit event at API startup or by the
  worker's 30-second maintenance loop. Physical deletion depends on an API or
  worker process running.
- Approval decisions and their action invocation are synchronous. The seeded
  local policy permits self-approval because identity is a fixed demo
  principal; production must use real reviewer identities and a policy that
  forbids self-approval. A database compare-and-set allows only one process to
  win a pending approval. The decision plus `EXECUTING` state and terminal
  invocation plus audit are transactional; the external Jira write cannot be
  in the database transaction, so production still needs idempotent connector
  actions and reconciliation for an uncertain result. Startup and the worker
  mark `EXECUTING` invocations older than two minutes as `TIMED_OUT`, record an
  `action.execution_interrupted` audit event, and explicitly mark the outcome
  unknown and non-retryable; this prevents an automatic duplicate write but
  does not determine whether Jira accepted the original request.
- This adapter is suitable for local v0 evaluation, not customer data or a
  production multi-tenant deployment.

MFA, SSO/OIDC, refresh tokens, and account recovery remain follow-up identity
work. Managed identity
and production deployment hardening remain outside this local-v0 runtime.

## PostgreSQL Integration Checks

The default test suite uses in-memory repositories and SQL recording fakes.
Cross-process persistence checks live in `tests/integration` and need
an empty or disposable PostgreSQL database whose role can create and drop
schemas. Each check creates a random isolated schema and drops it afterward:

```sh
WEAVES_TEST_DATABASE_URL='postgresql://user:password@localhost:5432/weaves_test' \
  .venv/bin/pytest -q tests/integration
```

Without `WEAVES_TEST_DATABASE_URL`, these tests skip. They exercise rate-limit
sharing across independent connection pools, concurrent approval
compare-and-set behavior, run and queue idempotency, single-worker claiming,
trigger and schedule races, and evaluation suite/report persistence. The
queued-evaluation integration test also races submissions from separate runtime
pools, then executes the winning job from the other pool and reloads its
per-case results and report through the submitting pool.
