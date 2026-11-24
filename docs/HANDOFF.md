# Weaves backend: engineering handoff

Status date: 2026-11-22. Audience: the engineer who takes over Phases 5 to 8 (usage limits and
fairness, recovery and release readiness, plans and the payment gateway, enterprise identity) and
anyone reviewing security, bugs, optimization and the data model. Read this first, then the
documents it points to. Everything here was checked against the code at the time of writing; where
something is a decision rather than a fact it says so.

## 1. What this system is

Weaves is a multi-tenant AI agent platform: tenants (organizations) own workspaces, agents,
workflows, tasks and connectors, and run durable work through a separate worker process. This
repository is the backend (API, worker, storage, tenancy harness). The frontend lives in
`weavelabs-pvt-ltd/weaves-app`; see its `docs/HANDOFF.md`.

**Current mode: internal only.** The product is used by its owner. There is no public signup, no
external customers, and the data is not valuable. The owner has explicitly accepted breakage, said
backups are not required for now, and wants no legacy or compatibility code. Public signup stays off
until the Phase 6 gates pass. Treat "internal mode" as a deliberate simplification, not a permanent
operating model.

## 2. Where things are

| Thing | Location |
|---|---|
| Backend repository | GitHub `weavelabs-pvt-ltd/weaves` (default branch `main`) |
| Frontend repository | GitHub `weavelabs-pvt-ltd/weaves-app` |
| Production | One VM `weaves-personal` (GCP project `weaves-510819`, zone `asia-south1-a`), Docker Compose project `weaves-single` (PostgreSQL 16, API, worker, static frontend, Caddy). Public host `app.weave-studio.in` |
| Access | SSH only through IAP (`gcloud compute ssh ... --tunnel-through-iap`) with the account named in `infra/gcp/singlevm/README.md`. No public SSH. |
| Images | Artifact Registry in `weaves-510819`; releases are immutable digests |
| Deployment | Automatic: every push to `main` in either repo runs checks, builds an image and deploys it to the VM through GitHub OIDC and Workload Identity Federation (`.github/workflows`, `infra/gcp/singlevm/deploy.py`, `apply_release.py`). Pull requests only verify. |
| Operations runbook | `infra/gcp/singlevm/README.md` |
| Plan and history | `MULTI_TENANCY_IMPLEMENTATION_PLAN.md` (the authoritative plan with one "progress" paragraph per delivered slice), `TENANCY_ARCHITECTURE.md`, `MULTI_TENANCY_AUDIT.md` (the original audit, historical) |
| Data model | `docs/DATA_MODEL.md` (code-derived catalogue, ER diagrams, physical schema) |

Merging to `main` deploys to production. There is no staging environment yet; the real-stack test
harness (section 6) is the substitute.

## 3. Architecture in one page

**Processes.** `apps/api` (FastAPI) and `apps/worker` (native worker loop) run as separate
containers from one image, sharing PostgreSQL. The worker claims durable jobs and task turns,
dispatches schedules and webhooks, and runs recovery and the organization purge ticks.

**Code layout.** `contracts/v1` (pydantic contracts, `extra=forbid`), `application` (ports, runtime
composition), `modules/*` (auth, agents, workflows, tasks, models, integrations, governance,
executions, skills, evaluations), `infrastructure` (PostgreSQL and in-memory repositories, database
setup, secrets, HTTP), `clients` (model providers and connectors), `apps` (API and worker entry
points), `shared`, `tenancy_harness` (real-stack tests), `infra/gcp/singlevm` (production ops).

**Identity and tenancy.**
- Sign-in is **Google OpenID Connect only** (authorization code with PKCE, state and nonce, verified
  ID token). There are no passwords, no email codes, no SMTP, no invitations and no tenant-level SSO.
  Code: `modules/auth/google_*`, `modules/auth/http/google_routes.py`.
- A global **Account** (verified identities) has **Memberships** in organizations; each membership
  points at the existing tenant-local User and Principal, so roles, audit and ownership are
  unchanged. Accounts hold no permission by themselves.
- Browser sessions are an HttpOnly `__Host-weaves_account_session` cookie plus an
  `X-Weaves-CSRF` header and an allowed `Origin`. Non-browser tools can use account bearer sessions
  (`wvs_acct_`), which can only be minted by the operator script.
- Every tenant request names its organization in `X-Organization-Id`; there is no server-side
  "current organization". Access is re-derived from the active membership, user, principal and
  organization on each request (`modules/auth/account_selection.py`, `apps/api/authentication.py`).
  Refusals are deliberately indistinguishable (`403 {"detail":"Organization not available"}`).
- Machine access: service accounts and API credentials stay tenant-bound; a static development
  token mapping exists for local use.
- **Organizations** have settings and an explicit owner (`organization_settings`), a lifecycle state
  machine (active, suspended, deletion_requested, deleting, deleted), export, and a resumable purge.
  Operator-only actions are scripts run on the VM, never HTTP.

**Storage.** Almost everything is JSON documents in one PostgreSQL table, `product_records`
(`collection`, `record_id`, `org_id`, `workspace_id`, `payload jsonb`, ...), behind a repository
abstraction. **PostgreSQL row-level security** isolates tenants even if code forgets a filter: the
application connects as the restricted role `weaves_app` (`WEAVES_DB_APP_ROLE`), binds the
organization and a named "system purpose" per transaction, and cross-tenant work (sign-in lookup,
queue claims, recovery, dispatch, purge...) is limited to explicit collection allow-lists in
`infrastructure/database/row_security.py`. See `docs/DATA_MODEL.md`.

**Tenant context.** In durable processes, missing tenant context raises (no implicit local scope);
development and in-memory mode keep an explicit local identity (`local-developer`), which is only a
test convenience.

## 4. What is delivered (Phases 0 to 4)

Each item has a progress paragraph in `MULTI_TENANCY_IMPLEMENTATION_PLAN.md`.

| Phase | Delivered |
|---|---|
| 0 | Real-stack test foundation (PostgreSQL, API, native worker, Chromium, fake providers), ownership inventory (`tenancy_harness/ownership.json`) |
| 1 | Connector descriptor isolation, model egress containment (DNS pinning, redirects, proxies), lifecycle guard for work (suspended/archived stops everything), browser teardown and namespaced drafts, per-tenant sign-in throttles (since removed with the legacy flows) |
| 2 | Mandatory tenant context, immutable record ownership, tenant-owned encrypted secrets, cross-tenant reference validation matrix, tenant-keyed client-chosen IDs, PostgreSQL row-level security enabled in production |
| 3 | Accounts and memberships, org selection per request, cookie sessions with CSRF, account-linking rules, onboarding conversation, Google-only sign-in (legacy sign-in fully removed) |
| 4 | Verified atomic idempotent organization creation, organization settings and explicit ownership transfer with last-owner safety, lifecycle (suspend/resume/deletion request/cancel/purge), tenant export, schedule timezones with DST rules. Legacy schema purge (migration 22, deployed 2026-11-22: stale rows, indexes and the SSO trigger of the removed sign-in flows) |

Not covered by Phase 4 on purpose: time-limited operator support access (operators act only on the
VM), deleted data in older backups (Phase 6), and public signup.

## 5. What is NOT done: your work (Phases 5 to 8)

The plan text is in `MULTI_TENANCY_IMPLEMENTATION_PLAN.md` (sections "Phase 5" to "Phase 8",
"Migration and deployment protocol", "Testing matrix"). Below is what I would do and the hooks in the
code that exist today. These are suggestions, not decisions already made.

### Phase 5: usage limits, fairness, scale
- **Limits to define:** requests, seats, queued/running work, tool calls, storage, schedules, usage
  and spend, plus global emergency limits. Today the only limit is the per-account organization
  creation cap (`WEAVES_ACCOUNT_ORGANIZATION_LIMIT`, counted from `account_organization_requests`).
- **Admission points:** job and task enqueue (`modules/executions/runtime_queue.py`, task creation
  in `modules/tasks`), workflow triggers and schedule dispatch (`modules/workflows`), model calls
  (`clients/model_providers`), connector invocations. Admission must be atomic across API and worker
  processes: use the existing per-organization advisory lock pattern
  (`ProductPostgresStore.acquire_advisory_transaction_lock`) or a counter row with a unique index.
- **Fair scheduling:** `claim_next_queued` (`infrastructure/persistence/postgres_query_repository.py`)
  claims oldest-first across all tenants, so one busy tenant can starve the rest. Fairness needs
  per-tenant concurrency caps and a rotating or weighted claim.
- **Usage accounting:** run/turn records carry usage; there is no tenant-level ledger. Design
  reservations that settle idempotently and survive crashes (cancellation, retries, unknown
  outcomes). Keep BYOK (customer-supplied provider keys) distinct from platform-paid usage.
- **Hot paths with full scans** (see `docs/DATA_MODEL.md`): several services still use unscoped or
  `.list()` scans and in-process filtering; collection and history APIs need bounds; recovery scans
  should become due/status queries with checkpoints. Home metrics should be aggregated server-side.
- **Observability before load tests:** structured tenant, request and job correlation in logs and
  metrics, without secrets or private payloads.
- **Gate:** concurrent admissions never exceed a limit; a noisy tenant cannot hurt another within a
  declared envelope; restart during reservation or settlement reconciles without double charges.

### Phase 6: recovery, release readiness, private beta
- Backups: a cron job writes daily dumps on the VM and `check_backup.sh` verifies a restore into a
  disposable database. The owner currently does not require backups (internal mode); for a beta you
  need off-host, encrypted, retention-checked backups and a tenant-selective restore, plus
  deletion tombstones so a restore cannot resurrect deleted tenants or revoked credentials.
- Release: both repositories' pipelines already run the real-stack suite against pinned counterpart
  revisions. Missing: staging, rollback rehearsal against real old images, minimum rollback target,
  alerts, tenant kill switch, runbooks, supported capacity.
- Public signup stays off until these pass. Production smoke must be read-only except
  pre-authorized synthetic tenants.

### Phase 7: plans, subscriptions, payment gateway
The gateway is **not chosen** and nothing is built. Requirements from the plan: plans and
entitlements over the Phase 5 usage model, subscription lifecycle, invoices, signed and idempotent
billing events, proration and grace rules, reconciliation, customer billing UI; payment state must
never be trusted from frontend redirect parameters.
Notes from this codebase:
- **Webhooks:** reuse the pattern of the signed workflow webhook route
  (`modules/workflows/http/automation_routes.py`: public path, secret digest, event idempotency
  key). A payment webhook must verify the provider signature over the raw body, store the event id
  with a unique key before acting, and be reorder-safe.
- **New data:** billing customer, subscription, invoice, entitlement, usage ledger and event
  collections. They are organization-scoped except provider event inbox rows, which are global and
  need an explicit system purpose and allow-list in `row_security.py` plus an `ownership.json`
  entry (a test fails otherwise).
- **Enforcement:** non-payment should use the existing organization lifecycle (suspend through
  `modules/auth/organization_lifecycle.py`) rather than a new mechanism; entitlements feed the Phase 5
  limits.
- **Money:** store amounts as integer minor units with an explicit currency; never floats.
- **Secrets:** provider credentials go through the tenant-owned secret store with a purpose tag.
- Test with provider sandbox events: duplicate, delayed, reordered, failed, forged and replayed.

### Phase 8: enterprise identity
Org-bound OIDC connections (the old tenant SSO code was deleted; do not resurrect it, build on
accounts), verified domains, SSO-only policy enforced on every session-creation path, session policy
revocation, optional MFA and SCIM. The Google sign-in implementation (`modules/auth/google_*`) is a
good reference for state, nonce, PKCE and ID-token verification.

## 6. Testing and quality gates

**Never skip the real stack.** Unit tests alone missed real defects repeatedly (a logout that revoked
the wrong session, a merge that left a browser test file unparseable, workspace selection after
sign-in). The real-stack suite is the main safety net.

| Layer | How to run |
|---|---|
| Lint, types, size | `ruff check --exclude .claude .` (**check the exit status**: "unsafe fixes available" still means failure), `ruff format --exclude .claude .`, `mypy`, `python scripts/check_python_size.py` (500 lines per file, 80 per function, complexity 10, no `noqa`) |
| Unit and contract | `pytest tests -q` |
| PostgreSQL integration | `WEAVES_TEST_DATABASE_URL=postgresql://user:pw@host:port/db pytest tests -q` (each test creates its own schema). Includes tests under the restricted `weaves_app` role. Without the variable ~200 tests skip. |
| Real stack | `python -m tenancy_harness.run --backend . --frontend <weaves-app checkout at the revision in tenancy_harness/pair.json> --candidate backend --allow-dirty` (Docker, Python 3.12+, Node, Playwright Chromium; 8 to 15 minutes). Required controls are listed in `tenancy_harness/pair.json` (`T0-*` foundation, `T1-*` isolation and accounts, `T4-*` Phase 4) and the run passes only if all are true. |
| Paid AI stage | add `--live-anthropic-env <env file>`; spends real money through a budget-gated relay |
| Local gate | `scripts/check_local.sh` (lint, format, size, types, harness syntax, unit tests); `scripts/install_git_hooks.sh` runs it before every push |
| CI | `.github/workflows/ci-deploy.yml`: `verify` (lint/types/size/tests with PostgreSQL) runs on pull requests and on main; the slow real-stack job runs on **pull requests and manual runs only** (it is not repeated on the merge commit); deploy runs on main after `verify` passes and `tenancy` has passed or been skipped. A new push to a pull request cancels its older runs |

Environment notes: the repo `.venv` is Python 3.9 and unusable; use Python 3.14 (or 3.12+). The tests
need PostgreSQL 16 (a disposable container is enough). The machine can run out of memory when Docker
builds and Chromium run together: a Playwright run that dies with "Target page, context or browser
has been closed" is usually that, so rerun it.

**Paid AI testing budget.** `AGENTS.md` fixes a USD 100 lifetime budget in
`~/.config/weaves-testing/` shared across all runs. Never reset it, bypass the relay or raise the
cap; at last report about USD 99.7 remained. A live run costs about one cent.

**Cross-repo pinning protocol** (easy to get wrong): `tenancy_harness/pair.json` pins the counterpart
revision. The harness is authored in this repo and vendored byte-for-byte into the frontend with
`python scripts/sync_tenancy_harness.py --frontend <dir containing package.json>`, which also
rewrites `bundle.lock.json`; run it after **every** harness edit or `tests/tenancy` fails.
Pinned frontend revisions may be pre-squash PR commits: fetch them by hash. After a frontend PR is
squash-merged, re-pin to the merged commit. Also run `node --check` on every
`tenancy_harness/*.cjs` after merging (a merge once left a control file unterminated and the browser
stage ran zero tests while the error looked unrelated).

## 7. Operations

Production is changed by merging to `main`. Operator scripts live in `infra/gcp/singlevm/` and are
streamed into the running API container (never exposed over HTTP); see the README there for exact
invocations:

| Script | Purpose |
|---|---|
| `bootstrap_account_owner.py` | create an account (verified email), organization and membership; idempotent |
| `issue_account_session.py` | break-glass: print a short-lived account bearer session for an existing account |
| `organization_lifecycle.py` | suspend, resume, request/cancel deletion, purge-now, status, list (audited, operator named) |
| `backfill_schedule_timezones.py` | write an explicit zone into legacy schedules (dry run by default) |
| `backup.sh`, `check_backup.sh`, `verify_restore.py` | backup and restore verification |

Key environment variables (set in the VM's root-only `runtime.env`; secrets never go in Git):
`DATABASE_URL`, `WEAVES_SECRET_ENCRYPTION_KEY` (provider-key encryption; losing it loses stored
keys), `WEAVES_BROWSER_SESSION_SECRET`, `WEAVES_GOOGLE_CLIENT_ID` and `_SECRET`,
`WEAVES_ACCOUNT_ALLOWED_EMAILS` / `_EMAIL_DOMAINS` (who may sign in; production is restricted to the
owner's email), `WEAVES_PUBLIC_FRONTEND_URL` and `WEAVES_PUBLIC_API_BASE_URL`,
`WEAVES_DB_APP_ROLE=weaves_app` (turns on row-level security; enabled in production),
`WEAVES_SEED_LOCAL_DEFAULTS=false`, `WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION` and
`WEAVES_ACCOUNT_ORGANIZATION_LIMIT`, `WEAVES_ORGANIZATION_DELETION_GRACE_DAYS`,
`WEAVES_ORGANIZATION_EXPORT_MAX_BYTES`, worker intervals `WEAVES_WORKER_POLL_SECONDS`,
`WEAVES_SCHEDULE_POLL_SECONDS`, `WEAVES_LIFECYCLE_POLL_SECONDS`. Google OAuth consent screen is in
Testing mode, so only listed test users can sign in; the application allowlist is a second gate.

Rollback and migrations: migrations are append-only and forward-only (`infrastructure/database/store.py`; head is
derived from the list, currently 31); internal mode does not maintain reader-rollback compatibility, so images older than
migration 31 cannot be rolled back onto this database (`min_rollback_schema_version` in `infra/gcp/singlevm/release_policy.json`). The plan's "Migration and deployment
protocol" describes the stricter expand/contract process you should adopt before real customers.

## 8. Decisions worth knowing (and why)

- **One JSONB table, no foreign keys.** Chosen for fast iteration; uniqueness is enforced with
  partial expression indexes and isolation with row-level security. The architect review should
  question this (see `docs/DATA_MODEL.md`).
- **Google only.** No SMTP or codes: Google proves identity, and the product is internal.
- **Account plus membership, not global users.** Existing tenant-local users and principals keep
  every ID, role binding and audit reference; accounts are only the login layer.
- **Per-request organization, no "current organization".** Two tabs can work in different
  organizations safely.
- **Indistinguishable refusals.** Unknown, foreign, suspended and deleted organizations look the same.
- **Operators are not HTTP.** Operator powers exist only as VM scripts (attributable, audited);
  customer Admin and Owner roles cannot reach them.
- **Deletion is a grace period then a resumable purge**, retaining only a tombstone, the lifecycle
  history, lifecycle audit events and the creation-quota ledger (rationale in
  `modules/auth/organization_retention.py`).
- **Timezones:** instants are UTC; calendar rules run in the schedule's zone; spring-forward gaps run
  at the first valid instant, fall-back overlaps run once; a legacy schedule reads as `Asia/Kolkata`;
  a zone-only change keeps the pending run and applies from the next one.

## 9. Known issues and deferred work

- No staging environment, alerts, kill switch or off-host backups (Phase 6).
- The chooser can now hide "Manage deletion" from non-owners: each membership on the account views
  carries `is_owner` (the frontend change that uses it is separate).
- Lifecycle views say "the organization owner" or "operator", never a person's name.
- Non-admin members see no organization-timezone default in the schedule editor (the server default
  still applies); needs a small read endpoint.
- Deleted data persists in older backups.
- An in-flight worker's last write after a purge is mitigated (lease fencing, activity checks,
  repeated empty passes) but not blocked by the database.
- `/account/oauth` start has no rate limit (it writes one short-lived row per call).
- The memory-only development mode serves an implicit `local-developer` identity because many tests
  rely on it; durable mode never does.
- Frontend `dashboard.js` is ~3.7k lines of mixed concerns: a split is advisable before more features.
- Several hot paths still scan collections (see section 5, Phase 5).

## 10. Reviews planned by the owner

1. Security, bug and backend/frontend optimization pass.
2. ER and database schema review by an architect: start from `docs/DATA_MODEL.md` (it lists
   candid gaps, unused objects and a question checklist).
3. Then Phases 5 to 8 and the payment gateway, by a different engineer (you).

## 11. Documentation index

`docs/HANDOFF.md` (this), `docs/STATUS_AND_BACKLOG.md` (what is done and what remains), `docs/DATA_MODEL.md`, `MULTI_TENANCY_IMPLEMENTATION_PLAN.md`,
`TENANCY_ARCHITECTURE.md`, `tenancy_harness/README.md`, `infra/gcp/singlevm/README.md`, `README.md`,
`QUALITY.md`, `AGENTS.md`; older product notes in `docs/` are historical context and may describe
removed flows.

## 12. Access the next engineer needs from the owner
GitHub access to both repositories; a GCP login with IAP access to project `weaves-510819` (the VM has no public SSH);
the Google OAuth client for `app.weave-studio.in` (the owner keeps the client file; never commit or paste it); their own
paid-AI testing budget (the shared ledger lives only on the owner's machine under `~/.config/weaves-testing/`);
and the owner's decisions listed in `docs/STATUS_AND_BACKLOG.md` section E2 (payment gateway, pricing, retention, legal).
Add new documents under `/docs/` with `git add -f` (the directory is gitignored but tracked files exist).

## 13. Handover to Snigdh (2026-11-22)
Snigdh (`snigdhchandra23@gmail.com`) takes over technical ownership. Start with `docs/STATUS_AND_BACKLOG.md` sections H and I (what is left, product decisions, production facts), then the open draft PR in `weaves-app` (`codex/frontend-shared-components`, with `docs/handoff-specs/`). He is on the production sign-in allowlist and needs GCP IAM on project `weaves-510819` (Compute Viewer, IAP-secured Tunnel User, and Compute OS Admin Login or Instance Admin for `sudo`); the owner grants access. The Google OAuth client file stays with the owner and must never be committed.
