# Multi-tenancy implementation and release plan

Planning baseline: 2026-11-20. Based on [the tenancy audit](MULTI_TENANCY_AUDIT.md), backend `d70176dfbe3c328e6e3070fa2f79f5c123a23db2`, and frontend `6f6ed9888b398921e2d567e0671b954df64185e8`.

Status: Phase 0 PRs #3/#15 and connector containment/ownership PRs #4/#16/#5/#17 are merged. Production API/worker and frontend were verified after migration 20 activation on 2026-11-20, including a restored backup comparison and ten authenticated pages. The private frontend checkout credential is configured and backend CI/deployment run 44758652570 completed successfully. The current `codex/model-egress-containment` slice implements model-provider egress protection and is not deployed yet. Lifecycle eligibility, browser teardown/drafts and tenant-specific authentication/throttling remain open.

### Phase 1 progress

The first containment slice restricts legacy imported connector descriptors and tool schemas to the one organization established by their installation references. Only the three built-in connector identities are public. Multiple same-org workspaces and disabled references preserve ownership; orphaned and cross-org conflicting definitions are hidden. Capability resolution, tool dispatch, health checks, credential replacement, updates, and disconnects enforce the same visibility rule. Conflicting references cannot be deleted through ordinary disconnects, which could otherwise silently assign the schema to the remaining organization; operator resolution is required.

The first containment slice preserves schema version 19 and the existing descriptor JSON shape. Its ownership migration follow-up has since been deployed and activated. Model egress is implemented in the current slice below; lifecycle eligibility, browser teardown/drafts and tenant-specific authentication/throttling remain open.

The original leak was reproduced by a failing two-org HTTP regression before the fix. Backend tests, native PostgreSQL restart/ambiguous-owner cases, and the real-stack Chromium/API control `T1-I01` verify the containment. `T0-D01` is removed from diagnostic observations and mapped to that required passing isolation control. Four unresolved diagnostics remained at that checkpoint; the current egress slice replaces a second diagnostic with a required control. `saas_ready` stays false.

Containment checkpoint validation: 370 backend tests passed, strict mypy passed on 279 source files, and all 16 real-stack cases passed. A three-call real Anthropic run passed the stricter agent-reply, approval/continuation, and cross-tenant browser checks, with verified skills/citations/tool provenance. An earlier live attempt completed its workflow but failed the required response-marker assertion; it was counted as failed and charged to the ledger. The harness now retains scoped synthetic model responses privately before assertions/cleanup and never accepts a marker found only in the user's message. At that checkpoint, lifetime conservative usage was USD 0.0433764 across 11 requests, with USD 99.9566236 available and no unresolved holds. This testing does not establish Phase 1 completion or SaaS readiness.

### Persisted connector ownership slice

`PluginCatalog` now provides scoped SQL reads and explicit owner writes without changing descriptor JSON. New connections preserve their organization independently of installation references. The migration report classifies legacy definitions as public built-ins, one proven owner, or immutable quarantine; missing descriptor references are reported separately. Disabled references count, and conflicting/ownerless/invalid definitions are never assigned to another organization to complete a backfill.

Application startup remains reader-only at schema 19. After the compatible API and worker image is deployed and verified, an operator may explicitly activate migration 20 using the procedure in `TENANCY_ARCHITECTURE.md`. The migration is atomic, idempotent, and refuses stores over 10,000 total records; larger stores need a separately rehearsed concurrent-index/batched migration. It preserves all business JSON, records the immutable reader image, and adds guards against owner rewrites, foreign references, and deletion/creation races. A brief exclusive table lock with bounded timeouts keeps catalog version checks and queries consistent across activation; plan a maintenance window. Startup refuses missing/disabled guards or incomplete activation. After activation, scoped connection cleanup can remove legacy conflicting references while the descriptor remains quarantined.

Ownership development validation included 386 passing backend tests with actual PostgreSQL, including 13 migration/concurrency cases, strict mypy on 285 source files, 17 required real-stack cases, and fake-model skills/tool/approval provenance. The suite tests a warm schema-19 API before activation, a reader overlapping activation, migration 20, newly started native workers, new owned connections, foreign-reference denials, and quarantine after browser cleanup. The subsequent authorized production release activated migration 20 after backup and reader verification; it did not reset data.

The prior 256-token live test allowance produced paid `model.empty_response` failures on Haiku 5.5 after successful tool calls. Provider guidance confirms adaptive thinking can consume that allowance before text. The live envelope is now 1,024 tokens and still fits its unchanged USD 1 conservative reservation; the lifetime cap remains USD 100 and over-limit/unpriced calls are blocked before dispatch. The final real Haiku/Sonnet task and approval workflow passed with pinned skills, citations, positive usage, and successful connector provenance. Failed paid attempts remain in the ledger; conservative lifetime usage at that checkpoint was USD 0.0541974 across 16 requests, with USD 99.9458026 available and no unresolved holds.

### Model-provider egress slice

Hosted provider URLs are canonical, and custom base URLs require operator approval. A dedicated, directly connected HTTP pool checks DNS at the connection boundary, pins a permitted address, retains hostname verification and refuses redirects/proxies. It guards new registration, credential checks, rotation, catalog refresh, legacy records, environment defaults and task/workflow completion. Operator private-network exceptions are exact-host configuration; link-local, loopback, mapped/transition and metadata addresses remain blocked. There is no contract/schema migration or production configuration change.

Validation: 455 backend tests passed with actual PostgreSQL; strict mypy passed on 287 source files; Ruff, formatting, size and frontend lint/syntax checks passed.

`T1-I03` replaces `T0-D02`. The required real-stack suite has 14 passing isolation controls and three explicit remaining diagnostics (17 cases total). The new control proves zero probe dispatch from rejected API requests and a native-worker failure with durable `model.egress_denied`, alongside valid tenant-scoped credentials, verified TLS, redirect denial and proxy independence. Fake task/skill/tool/approval provenance and three real Anthropic calls passed. Haiku and Sonnet used canonical URLs, positive usage, pinned skills, citations and actual approval continuation. The isolated provider reaches the separate budget relay over an internal bridge; only the relay joins the external network.

The real egress test added USD 0.0094100 in conservative accounting across three requests. Lifetime accounted usage is USD 0.0636074 across 19 requests, with USD 99.9363926 available and no unresolved reservations. The exhaustive cap remains USD 100; no pricing, cap or ledger reset occurred. All three other Phase 1 boundaries remain open, and SaaS readiness is still false.

### Lifecycle eligibility and tenant sign-in slice

Authorization now rejects suspended organizations and archived workspaces with a dedicated `TenantInactive` error, so signed webhooks (HTTP 403, no job created), schedule dispatch, worker turns, approval decisions and OAuth continuations stop at the next authorization boundary. Schedules skip the occurrence with an audit event instead of failing, so they resume after restoration and cannot starve other schedules. Running jobs observe an inactive tenant at the existing cooperative-cancellation boundary and finish as audited cancelled work. Administrators can still restore an archived workspace because that action uses organization-level authorization. Already dispatched external actions are not retried; interrupted work is still recorded by the existing recovery path.

Login throttles for password and email-code paths are namespaced by organization when an organization is selected, so failed attempts in A no longer consume the same email's allowance in B. Source-address throttles remain global. Unscoped requests for an email that exists in several organizations still receive no code. `T1-I04` replaces `T0-D03` and `T1-I06` replaces `T0-D05`; `T1-I05` replaces `T0-D04` (browser teardown, stale-response generations and namespaced drafts); no baseline defects remain.

### Phase 2 progress: mandatory tenant context

Durable (PostgreSQL) processes that do not seed local demo data now call `require_tenant_context`. In that mode `effective_org_id`, `effective_workspace_id`, `effective_principal_id` and the `set_request_principal` compatibility helper raise `TenantContextRequired` instead of selecting `local-org`/`local-workspace`. Development and in-memory test runtimes keep the explicit local scope. The required real-stack suite passes with the mode active in the API, native worker and CLI. Repository writes are also ownership-immutable: `put` can no longer rewrite `org_id`/`workspace_id` on conflict (it raises `OwnershipConflict`), and the fenced conditional updates only match a row with the same owner, in PostgreSQL and in memory. The one legitimate rescope, the local developer's grant becoming organization-wide, is now seeded that way (a legacy workspace-scoped row is replaced explicitly). Unscoped `get`/`delete`/`consume`/`list` remain for system and bootstrap callers; scoped-only tenant repositories, reference validation, scoped secrets and PostgreSQL RLS remain open Phase 2 slices.

### Phase 2 progress: tenant-owned secrets

Stored secrets now carry `org_id`, `workspace_id`, `purpose`, `owner_ref` and the encryption `key_id`. `get`, `replace` and `delete` only match the caller's organization (in PostgreSQL and in memory), unowned rows are unreadable, and `put` records the effective tenant, so a missing tenant context fails closed. Existing rows are backfilled at startup from the provider, connector and OAuth records that reference them, only when every referencing record belongs to one organization; conflicting or orphaned secrets stay unowned. Existing Fernet encryption is unchanged. `shared.request_context.system_scope` names narrow cross-tenant operations for the row-level-security slice.

### Phase 2 progress: PostgreSQL row-level security

Migration 21 (read-compatible since the previous release) creates the non-owner `weaves_app` role, a `weaves_rls_allows` policy function and a `tenant_isolation` policy on `product_records`; the encrypted secrets table gets the same policy. A process with `WEAVES_DB_APP_ROLE=weaves_app` assumes that role in every transaction and binds `app.org_id` and `app.system_purpose` transaction-locally in one round trip (`set_config(..., true)`), so a pooled connection never carries a tenant into another transaction, a rollback or an exception. Tenant rows are visible only for the caller's organization; strict processes without context see none. Cross-tenant work is limited to named system scopes with explicit collection allow-lists: `bootstrap` (startup seeding), `auth` (sign-in and token discovery), `queue` (job/turn claims), `recovery`, `dispatch` (schedules and signed webhooks), `oauth` (callback state), `catalog` (legacy descriptor ambiguity check) and `studio` (canvas worker). Onboarding writes a new tenant under that tenant's own scope. The owner connection used for migrations and operator tooling is unaffected (no FORCE), and the default deployment keeps the role unset, so the policies stay dormant until an operator opts in.

Evidence: the real-stack suite (API, native worker, CLI fixtures) and the live Anthropic run pass with the restricted role active; direct SQL tests under that role show reads, updates, deletes, inserts and ownership transfers against another organization are refused, strict no-context sees nothing, scope does not survive rollback or reuse of a single pooled connection, and system scopes expose only their listed collections. Point-lookup latency on 200,000 rows measured 1.9 ms with the policy against 2.5 ms (noise-level difference) as owner; scoped queries keep their index scans, and unscoped collection scans remain sequential as before. Not yet covered by policies: `product_plugin_ownership*` and `product_login_throttle`.

### Phase 2 progress: tenant-keyed client identifiers

Collections whose identifiers are chosen by clients (`evaluation_suites`, `evaluation_reports`) are now stored under `<org_id>:<id>`. Two organizations can use the same suite name without a conflict, and creating a name another tenant owns no longer reveals that it exists. Public identifiers and payloads are unchanged; reads accept both the new key and any legacy unprefixed row owned by the requesting organization, so no backfill is needed. A duplicate inside one organization still fails.

### Phase 0 implementation evidence

- [Architecture decisions](TENANCY_ARCHITECTURE.md), [complete ownership registry](tenancy_harness/ownership.json), and [portable test harness](tenancy_harness/README.md) are implemented. The registry covers 45 collections and 135 API operations, with drift checks.
- Backend validation: 350 tests passed, including real PostgreSQL integration tests and all budget guards; Ruff, formatting, size limits, and strict mypy on 277 source files passed.
- Existing frontend validation: 93 browser tests passed, with seven existing optional environment-dependent tests skipped; ESLint and syntax checks passed. The complete workflow regression file subsequently passed all 23 tests, including the new workspace-restoration deep-link regression.
- Required new real-stack suite: all 16 cases passed with zero skips, using PostgreSQL, separate native API/worker processes, SMTP email proof, HTTPS model/MCP fakes, and Chromium. Eleven are authentication/isolation controls; five explicitly reproduce open Phase 1 defects. Both backend-owned and frontend-vendored entrypoints were exercised.
- Opt-in real Anthropic testing passed all three live cases: a Haiku agent task, a two-stage Sonnet workflow with approval and continuation, and foreign-tenant denial. Native worker records verified provider request IDs, token usage, a pinned org skill, a successful read-only tool invocation, and a citation. The first attempt exposed a frontend deep-link race; a free fake-provider reproduction and regression test confirmed the fix before the successful live run.
- The exhaustive USD 100 lifetime testing budget persists outside both repositories. Eleven budget/relay tests passed, including concurrent admission, restart persistence, exhaustion before outbound dispatch, uncertain-charge holds, and invalid or insufficient cost reservations. At validation, five total model requests consumed USD 0.017752 under conservative 2x usage accounting; USD 99.982248 remains with no unresolved holds. No run resets the ledger or increases the cap; exhaustion requires explicit user authorization for additional funds.
- Both deployment workflows now require their `tenancy` job. The pinned private frontend checkout needs `TENANCY_FRONTEND_READ_TOKEN` as a backend Actions secret, restricted to frontend Contents: read. GitHub rejected deploy keys because they are disabled for that repository; no broad personal token was substituted. The missing credential causes a clear failure, not a skipped integration check.
- Phase 0 was published through paired `codex/tenancy-phase-0` branches and merged by the user. That initial private-checkout gate was later resolved; the current deployment evidence is recorded in Status above. PR #2 was separately reviewed and merged as `499a43ef7f9c3a4a3b6ac6db1a6cee9c218be466`, the Phase 0 backend counterpart pin. No production migration is part of Phase 0. The report explicitly says `saas_ready: false`; Phase 1 replaces each fixed diagnostic with a passing isolation control.

## Target and launch scope

Deliver pooled SaaS on the existing architecture: a shared application and PostgreSQL database, with tenant boundaries enforced through authorization, persistence, secrets, workers, and browser state. Retain the inexpensive deployment while measuring its capacity; separate databases and Kubernetes are not initial requirements.

Recommended first release: a small, controlled, BYOK private beta after phases 0–6. Verified self-service signup can then be enabled behind its launch flag. Subscription payments follow in phase 7; enterprise SSO enforcement and provisioning follow in phase 8. Do not expose an incomplete enterprise capability as supported.

### Identity and ownership decisions

| Concept | Target responsibility |
|---|---|
| Account | Global human identity, authenticated session, verified login identities. Has no implicit permission in any organization. |
| Organization membership | Links an account to one organization and its tenant-local user/principal; holds membership lifecycle. Unique per account/organization. |
| Tenant-local user/principal | Preserve existing user/principal IDs, role bindings, ownership references, task authorship, and audit history. Service principals remain tenant-bound and do not become human accounts. |
| Organization | Customer security/data/billing boundary; owns model configuration, memberships, settings, and org skills. |
| Workspace | Operational partition inside an organization; owns agents, workflows, tasks, installations, approvals, and execution resources. |
| Personal content | Private to its owner inside its tenant; a global account does not make personal skills visible to its other organizations. |
| Public catalog | Explicitly global, operator-controlled built-in definitions. Imported customer connector metadata is tenant-owned. |
| TenantContext | Validated org/workspace/principal/account or service identity, with authorization/lifecycle information; required for normal tenant operations. |
| System access | Explicit narrow ports for authentication discovery, queue dispatch, migrations, and maintenance; never implicitly available through customer Admin roles. |

Use a global account plus tenant memberships, rather than moving all business resources to global ownership. Preserve the existing org/workspace resource scopes. Credentials and secrets must not be shared automatically between organizations.

For browser sessions, prefer an account-authenticating HttpOnly cookie and explicit per-tab organization/workspace selection on each request, validated against current memberships. Do not put a mutable global “current organization” in a shared cookie/session and assume different tabs cannot interfere. Headers/route parameters identify a requested scope; they never prove authorization. Tenant API keys stay bound to their original tenant regardless of supplied headers.

Legacy accounts must not be merged solely because their email strings match. Existing email claims may be unverified, passwords may differ, and enterprise issuers have separate trust boundaries. Account linking requires an authenticated, verified linking flow and explicit handling of ambiguous legacy identities.

## Phase overview and dependencies

| Phase | Deliverable | Depends on | Release gate |
|---|---|---|---|
| 0 | Contracts, ownership map, adversarial fixtures, real integration CI | Audit | Repeatable baseline; current defects captured as explicit regressions |
| 1 | Fix confirmed leaks, egress, lifecycle, browser state, and ambiguous login | 0 | All targeted regressions pass; existing workflows remain compatible |
| 2 | Mandatory scope, ownership-safe storage, DB safeguards, tenant-owned secrets | 1 | API and direct DB isolation pass under production-style roles |
| 3 | Global accounts, memberships, browser sessions, organization switching | 2 | Same person can use two organizations without state/permission crossover |
| 4 | Verified onboarding, organization administration/lifecycle/settings | 3 | Provision/suspend/resume/export/delete rehearsals pass |
| 5 | Quotas, usage accounting, fair execution, bounded queries | 2–4 | Concurrent quota and noisy-neighbor tests pass at the declared beta envelope |
| 6 | Recovery, operational visibility, deployment gates, private-beta release | 0–5 | Full-stack tenancy suite and rollback/recovery rehearsal pass |
| 7 | Plans, subscriptions, payments, billing reconciliation | 5–6 | Billing and entitlement events are idempotent and isolated |
| 8 | Enterprise identity policy, SSO, optional provisioning/domain features | 3–6 | Provider and policy enforcement matrix passes end to end |

The phases are release milestones, not single large PRs. Split each into schema/reader preparation, implementation, and integration/rollout slices where necessary. Write failing regression tests in the same branch as their fixes; do not leave permanently skipped or expected-failing security tests on main.

## Phase 0 — Define the invariants and build the test environment

### Changes

- Produce an ownership/access map for every persisted collection and API family: global, account, org, workspace, or owner-private. Define allowed system callers and lifecycle behavior.
- Freeze identity/linking/session decisions above in an architecture decision record. Define suspended-tenant behavior, tenant admin versus operator authority, and compatibility guarantees.
- Introduce reusable fixtures with orgs A/B, two workspaces each, Admin/Builder/User memberships, a multi-org human, a single-org human, and a service account. Seed unmistakable tenant-specific sentinel content in each resource family.
- Start real PostgreSQL 16, API, worker, and frontend together in a disposable local/CI environment. Stub only external email/model/OIDC/connector providers. Do not replace product authorization or persistence with mocks in the integration suite.
- Make the cross-repo integration job use a recorded pair of backend/frontend revisions. Existing frontend Skills/Workflow integration specs skip when fixture environment variables are absent; the required tenancy job must fail on missing fixtures or skipped required cases.
- Preserve existing pytest, Ruff, formatting, mypy, size checks, frontend ESLint/syntax/Playwright, and image-build checks.

### Tests and exit gate

Reproduce all five immediate audit findings in the new harness; demonstrate allowed same-tenant actions and denied cross-tenant actions. Current baseline is the previously observed 317 passing backend tests, not a fixed required count. Establish a test inventory with stable test IDs and publish failures/traces without secrets. A clean checkout must run the fixture without production configuration or data.

Suggested suites: `tests/tenancy/`, `tests/integration/tenancy/`, frontend `tests/e2e/tenancy/`, and a versioned test-fixture manifest.

## Phase 1 — Close the confirmed boundaries

### Changes

- Separate tenant-owned imported MCP descriptors from global built-ins. Backfill ownership from installation references. Quarantine orphaned/conflicting custom descriptors for resolution; never classify them as public to keep a migration moving.
- Enforce canonical hosted-model URLs and a controlled custom-provider egress policy. Cover scheme/port, loopback/private/link-local/IPv6 addresses, DNS resolution/rebinding, proxy behavior, redirects, discovery, credential checks, and completion requests. Add deployment-level egress restrictions where applicable.
- Apply one tenant/principal/workspace eligibility guard to HTTP, worker start, signed webhooks, schedules, workflow continuation, OAuth continuation, and approval/action dispatch. Stop new work after suspension/archive; request cooperative cancellation of running work at safe boundaries. Record uncertain external outcomes without blind retries.
- Centralize frontend teardown for logout/expiry/scope changes: resources, builders, private caches, drafts in memory, dialogs, controllers, pollers, and pending requests. Add auth/scope generations so delayed successes and 401s from an old session cannot affect a new one.
- Namespace browser drafts by org/workspace/principal. Quarantine old workspace-only drafts rather than attributing them to the next person who signs in. Provide explicit recovery only when ownership can be established.
- Add an interim tenant-specific sign-in identifier for legacy password/code/SSO paths. Keep onboarding disabled during this work. Namespace tenant-account throttles while retaining global abuse/IP limits. Phase 3 replaces this interim journey with account/membership selection.

### Required tests

- A's descriptor name/tool schema is absent from B's list/search/detail; A's installation still works; built-ins remain public; foreign installation IDs remain denied.
- Rejected provider destinations cause zero outbound requests; canonical allowed destinations succeed. Test mixed DNS answers, rebinding/redirect handling, IPv6, encoded addresses, and proxy configuration at the connection boundary.
- Queue a task/run, archive the workspace or suspend the org, then attempt every trigger/worker/continuation/action path. No new model/tool call may start after the defined revocation boundary; blocked work reaches a deliberate, audited state.
- In one browser, switch users/workspaces with a builder, dialog, poller, and request open. Old content never appears, and an old 401 cannot expire the new login. Test two accounts sharing a workspace and two separate browser contexts.
- The same email can sign in to either explicitly selected tenant. Failed attempts for A do not consume B's tenant-account allowance; global abuse protections still apply.

**Exit gate:** all confirmed regressions pass in API, PostgreSQL, and actual browser tests where relevant. Release these containment fixes independently of the later account migration. Do not roll back to a version with a known leak as a routine fallback.

## Phase 2 — Enforce tenant ownership below the HTTP layer

### Changes

- Require validated TenantContext in production application services; remove silent `local-org` / `local-workspace` fallbacks outside an explicit development/test bootstrap.
- Separate tenant repositories from public-catalog and narrowly privileged system repositories. Require tenant predicates for reads, list/search/export, updates, deletes, consume, and conditional writes. Ownership is immutable on update/upsert.
- Validate referenced IDs within the same required scope: versions, agents, models, policy IDs, tools, threads, skills, members, and nested workflow structures. Add collection-appropriate nullability and ownership constraints; global records remain explicitly global.
- Add tenant/purpose/owning-resource metadata to secrets. Scope create/read/replace/delete, preserve existing encryption, and record key IDs for rotation. Backfill from scoped provider/installation/SSO references with orphan handling.
- Introduce PostgreSQL RLS incrementally for tenant collections, after separating global/auth/system operations. Use a non-owner application role without superuser/BYPASSRLS privileges and a separate migration role. Tenant settings must be transaction-local and reset safely on connection reuse.
- Give dispatch/recovery only the minimal bounded cross-tenant operations needed, then execute tenant work under normal tenant context. If controlled functions are used, restrict grants, fix search paths, validate inputs, and test their privileges. Do not give every worker unrestricted access to all tenant payloads.

### Required tests

- Parameterized matrix across every resource family and operation, with deliberate foreign IDs in both top-level and nested fields. Check both response and absence of database/external side effects.
- Direct repository and SQL attempts under the actual application role cannot read/write another tenant, transfer ownership, or access unowned secrets. Test no-context and forged-context behavior through the application boundary.
- Alternate A/B requests on the same pool, including rollback/exception/cancellation, nested transactions, concurrent work, and connection reuse. A's context must never survive into B's transaction.
- Authentication discovery, public catalog, global queue claim, and recovery still work only through their restricted system ports. Repeat migration and backfill safely; report unresolved/orphaned ownership.

**Exit gate:** isolation holds even when ordinary service code forgets a query filter; migration and privileged paths are tested separately. Measure scoped-query plans and latency before expanding RLS to all applicable collections.

## Phase 3 — Introduce accounts, memberships, and organization switching

### Changes

- Add global accounts, verified login identities, and account-to-org memberships; retain tenant-local users/principals and existing resource references. Make account/org membership uniqueness and identity keys database-enforced.
- Backfill legacy identity mappings without email-only merging. Produce conflict reports. Keep legacy tenant credentials restricted to their original tenant until ownership linking is proven; do not let one tenant's password unlock another tenant's membership.
- Separate account-level endpoints (session identity, membership discovery, verified organization creation) from tenant-level resource endpoints. Users without an active organization must still be able to authenticate and reach permitted account/recovery actions.
- Implement account browser sessions using the existing cookie/CSRF foundation, with same-origin app API routing. Keep account and legacy tenant session formats distinguishable. Rotate session credentials on sensitive authentication changes; audit login method and membership selection.
- Validate org/workspace selection on every request against current membership, principal status, org status, and workspace permissions. API/service credentials cannot select another org.
- Build the organization chooser/switcher, workspace switcher, no-membership state, removed-access state, and session expiry flow using existing UI design tokens, loader, and dialog components. Isolate caches by account/org/workspace and request generation.
- Keep selected organization per tab. Two tabs may safely work in different organizations under the same account cookie; one tab's switch must not silently change another tab's writes.

### Required tests

- One verified account uses A and B with different roles; an A-only account cannot discover or select B. Unknown and unauthorized tenant selections do not disclose protected data.
- Same-email legacy accounts with different passwords/issuers are not silently merged. Linking requires the documented proofs; a claimed/unverified email cannot take over an account or membership.
- Membership removal, org suspension, account disablement, and role changes affect subsequent authorized operations. A's suspension/removal does not unnecessarily revoke valid access to B.
- Password/code/social login, cookie issuance/expiry/revocation, CSRF, Origin checks, and service-key paths. Browser cookies must never authenticate on bearer-only routes and vice versa.
- Two tabs/two orgs, logout/login on the same origin, expired sessions, stale responses, deep links/bookmarks, personal skills, draft recovery, and cross-origin login callbacks.
- Migration rehearsals retain principal IDs, role bindings, skill ownership, audit attribution, task history, and existing tenant API credentials. Test supported old/new API/worker/frontend version combinations.

**Exit gate:** one identity can reliably use multiple organizations with independent membership and permissions. The old tenant-specific login remains only as a bounded compatibility path with an explicit retirement plan.

## Phase 4 — Deliver safe onboarding and tenant lifecycle

### Changes

- Require verified account authentication before organization creation. Provision org, owner membership/principal, workspace, roles, defaults, and audit atomically and idempotently. Return a human session/selection result, not a permanent owner API key.
- Keep member provisioning compatible with the existing invite-less email-code experience. A pending access grant may be claimed only through verified identity under the documented policy. Membership removal is different from disabling a global account.
- Add organization settings and administration: name/identifier, timezone/locale, membership/role management, owner transfer/recovery, lifecycle state, and feature/usage limits. Preserve last-owner/admin safety checks under concurrency.
- Separate platform operator actions from customer Admin roles. Customer admins manage only their org; operator access is narrowly scoped, attributable, and time-limited when used for support.
- Implement active/suspended/deletion-requested/deleting/deleted behavior, with reason and audit history. Suspend stops work through the phase-1 guard; resume does not automatically replay missed schedules or previously cancelled jobs.
- Add tenant export, retention-aware deletion, credential/OAuth teardown, and resumable purge of records, secrets, skills/snapshots, external identities, and queued work. Define which minimal audit/billing records must be retained and why.
- Store times in UTC and explicitly configure workflow timezone. Backfill existing schedules as `Asia/Kolkata` to preserve behavior; test daylight-saving cases for newly supported zones.

### Required tests

- Signup double-submit, retries, concurrent ownership/slug claims, unverified identity, expired proof, and injected failure after each provisioning step. No partially usable tenant or leaked credential remains.
- Two admins concurrently transferring/removing ownership cannot leave an invalid org. A customer Admin cannot invoke operator-only actions.
- Suspend/resume/export/delete with active tasks, approvals, schedules, private skills, OAuth setup, and provider keys. Operations are idempotent and resumable after crashes.
- Removing membership from A preserves B's membership and personal data. Export includes only authorized A data and excludes secrets by default.
- Deleted data cannot be resurrected by ordinary backup restore; retention/tombstone handling is exercised in phase 6. Timezone changes do not duplicate or silently shift existing scheduled occurrences.

**Exit gate:** full tenant lifecycle works in staging fixtures. Public signup stays off until phase 6 passes, even though onboarding functionality exists.

## Phase 5 — Isolate resource consumption and scale the hot paths

### Changes

- Define configurable tenant limits for requests, seats, queued/running work, tool calls, storage, schedules, and usage/spend where applicable. Maintain global emergency limits as well.
- Enforce quota admission atomically across API/worker processes. Reserve and settle managed usage with idempotency; handle cancellation, retries, crashes, unknown outcomes, and abandoned reservations. Keep BYOK usage/capacity limits even when Weaves does not pay model charges.
- Add fair tenant scheduling and per-tenant concurrency. Bound long operations, retry/backoff, queue age, and in-memory lock/cache growth. Ensure a hot tenant cannot monopolize the only worker indefinitely.
- Replace all-user email scans with indexed lookups; bound collection/history APIs; replace global full-history recovery scans with due/status queries and checkpoints.
- Add aggregate Home metrics and batched workflow summaries so the frontend does not traverse every task/history page to render an overview.
- Add structured tenant/request/job correlation and queue/usage/latency/error visibility before running load tests. Exclude secrets and private payloads; control metric cardinality.

### Required tests

- Simultaneous admissions at a limit never exceed it. Replays, cross-process retries, failed jobs, and cancellation neither double-charge nor leak capacity. Test the same idempotency key in different tenants and scopes.
- Saturate A with slow model calls/backlog while B submits normal work. Record per-tenant queue delay, completion latency, throughput, errors, pool usage, and memory. B must meet the beta service envelope declared before the run.
- Test large histories and thousands of records; verify pagination/summary query plans and bounded request/response counts. Polling and recovery remain bounded as tenant count grows.
- Restart API/worker during reservation/execution/settlement; reconcile safely without duplicate external writes or billing.

**Exit gate:** a written beta capacity envelope and measurements on representative deployment hardware, with enforced tenant caps. Choose limits from these results rather than promising an arbitrary tenant count or throughput. Increase capacity only when evidence requires it.

## Phase 6 — Prove release, recovery, and private-beta readiness

### Changes

- Make the required full-stack test environment run in both repositories' deployment workflows using pinned counterpart revisions and a recorded compatibility manifest. Include PostgreSQL, API, worker, frontend, migrations, and external provider fakes.
- Add schema/reader/worker/frontend compatibility checks and rollback rehearsals for each release that changes persistence or identity. Retain immutable image digests and the minimum supported rollback revision.
- Build tenant-selective export/restore rehearsal: restore the shared backup into an isolated recovery environment, extract the authorized dependency closure, validate ownership/conflicts, and import with deliberate conflict rules. Do not overwrite other tenants' newer production data to recover one org.
- Add off-host recovery appropriate to the beta, backup encryption/access controls, retention verification, deletion tombstones, and credential-revocation handling. A restored backup must not silently reactivate revoked credentials or deleted tenant data; require reconciliation/key rotation or deliberate session invalidation.
- Add alerts, tenant kill switch, audited operator procedures, incident/deletion/recovery runbooks, and a supported minimum deployment capacity. Keep tenant recovery and whole-service disaster recovery distinct.
- Release to controlled internal tenants, then a small private beta. Enable public verified signup separately only after its gates pass and the chosen capacity/abuse policy is active. Enterprise/billing features remain independently gated.

### Required tests and release gate

- Run the entire authorization/private-content matrix and browser identity-transition suite with no skipped required integration tests. Verify both successful customer journeys and deliberate attacks.
- Rehearse upgrade from a production-shaped dataset and rollback using actual compatible images. Preserve newly written data, sessions, references, and encryption keys across supported combinations.
- Exercise API/worker crash, network/provider timeout, database interruption, tenant saturation, restore, deletion-resume, and operator permission denials. Document expected recovery time/data-loss window from measurements.
- Staging smoke must use at least two tenants and multiple roles. Production post-deploy smoke is read-only except for pre-authorized, isolated synthetic-tenant probes; do not run destructive fixture tests against customer workspaces.

**Exit gate:** every phase 0–5 acceptance item is satisfied; all audit findings mapped to this milestone have evidence; compatibility/recovery and incident procedures are usable. This is the first recommended unrelated-customer SaaS release.

## Phase 7 — Add commercial SaaS when monetizing

### Changes

Add plans and tenant entitlements over the existing usage/admission model, subscription/customer lifecycle, invoices/payment-provider integration, signed/idempotent billing events, proration/grace-period rules, usage reconciliation, and customer billing UI. Payment status changes must affect only the correct organization and must not be trusted from frontend redirect parameters.

### Tests and gate

Use provider sandbox events for duplicate, delayed, reordered, failed, forged, and replayed webhooks. Test plan changes while jobs run, concurrent entitlement updates, cancellations, grace periods, and provider reconciliation after outages. Verify usage attribution cannot be reassigned across organizations and amount/currency arithmetic is exact. Paid launch requires ledger reconciliation and documented account/billing recovery behavior.

## Phase 8 — Complete enterprise identity and broader tenant features

### Changes

Wire existing SSO connection/external-identity foundations through org-bound OIDC start/callback transactions, stable issuer/subject identity binding, connection administration, tested activation, policy enforcement, session policy version/revocation, and recovery access. Add verified domains, MFA/SCIM if offered, and customer-specific branding/domains where justified. Shared SaaS OAuth applications remain valid; enterprise trust configuration is explicitly org-bound.

### Tests and gate

Test wrong issuer/audience/subject/tenant, forged or replayed state/nonce, expired transactions, callback tenant substitution, concurrent org sign-ins, disabled connections, secret rotation, and SSO policy changes with existing sessions. Enforce SSO-only policy across password/code/social-login/API/session creation paths with explicitly documented exceptions for service identities and emergency recovery. Verify SCIM changes affect only the intended membership, not the global account or another organization. Custom domain tests must prevent domain takeover and cookie/origin scope mistakes.

Enterprise features are released individually only after their full policy matrix passes. Optional features do not postpone the basic SaaS milestone.

## Migration and deployment protocol for every phase

The current JSONB contracts reject unknown fields, and migration readers explicitly recognize versions. An additive SQL change alone does not prove old application images can read new records. Apply this protocol to each affected record type and all API/worker/maintenance readers:

1. **Expand readers first:** deploy a compatibility release that understands the future schema/record shapes while new writers/features are off. Define its exact minimum rollback digest. Existing migration-19 compatibility does not authorize newer migrations automatically.
2. **Expand schema:** apply versioned changes through one serialized migrator with a separate role. Rehearse on a production-shaped disposable database and validate the actual rollback image against the future schema.
3. **Backfill:** checkpoint, batch, and verify counts/ownership/digests/references; support retries. Preserve legacy user/principal/resource IDs and immutable historical versions. Quarantine ambiguous identities/descriptors/secrets instead of guessing ownership.
4. **Enable new paths:** test backend-first deployment with the existing frontend, then the new frontend against its compatible backend. Record both SHAs/image digests/schema/flags in release evidence. No application may assume the other repo deployed simultaneously.
5. **Verify and observe:** tenant-scoped smoke, queue/usage/error checks, and business-data invariants. Disable a new feature if needed without reopening known isolation gaps.
6. **Contract later:** remove compatibility fields/paths only after old sessions/jobs/readers have expired or migrated and the rollback window has changed deliberately. Identity migrations may require forward fixes once new accounts exist; do not promise rollback to a reader that cannot represent them.

Image rollback must not automatically reset, drop, or restore the database. Keep all currently authorized customer data throughout the migration. Destructive retirement and actual infrastructure changes require their own concrete implementation/release work.

## Testing matrix shared by all phases

| Layer | Required coverage |
|---|---|
| Unit/domain | Ownership, membership, policy/lifecycle, linking, state transitions, quota/usage arithmetic, UTC/timezone rules. |
| HTTP contract | Authentication method, requested tenant, role, owner-private visibility, malformed/foreign/nested IDs, allowed action, denial status, no side effects. |
| PostgreSQL integration | Actual production-style roles/RLS, scoped CRUD, immutable ownership, pool reuse, uniqueness/idempotency, contention, migration and backfill retry. |
| Worker integration | Separate API/worker processes, durable queues, claims/leases, tenant lifecycle recheck, scheduling, approval continuation, crash/retry/cancellation. |
| Browser end to end | Real API and PostgreSQL, login/chooser/switch, two tabs/orgs, shared-browser users, drafts/private caches, delayed responses, dialogs/pollers, role-sensitive actions. |
| External boundaries | Fake email/model/OIDC/connector/payment servers; egress enforcement and redirect/DNS tests. No paid real calls in ordinary CI. |
| Load/reliability | Hot-tenant isolation, declared capacity envelope, bounded history/overview, restart/failure and reservation reconciliation. |
| Migration/release | Actual old/new image combinations, current data preservation, schema/read compatibility, min rollback target, secrets/keys, restore/delete/revoke reconciliation. |

For each tenant-scoped resource test: A has access to A where permitted; B cannot read or mutate A; a forbidden same-org role/owner cannot access private A content; a permitted role still works. Checking only for a 403/404 is insufficient—verify durable state and external call counts too.

Required pipeline behavior: fail closed on a missing PostgreSQL service, absent real integration fixture, unexpected required-test skip, leaked production configuration, or unsupported counterpart revision. Publish sanitized test inventory, image/revision/schema manifest, and failure traces. Avoid making the whole suite depend on third-party availability.

## Mapping all audit findings

| Audit finding | Phase(s) |
|---|---|
| 1 Custom descriptor leak | 1 ownership fix; 2 DB enforcement |
| 2 Unsafe model egress | 1 policy and transport/deployment boundary; 6 release validation |
| 3 Background work after archive/suspension | 1 shared guard; 4 lifecycle; 5 cancellation/accounting |
| 4 Frontend private state/drafts | 1 teardown; 3 account/org/tab behavior |
| 5 Duplicate-email login/throttles | 1 interim scoped login; 3 verified identity/memberships |
| 6 Account memberships/org switching | 3 |
| 7 Unverified onboarding/permanent owner key | 4, launch-gated in 6 |
| 8 Enterprise authentication | 3 browser/session foundation; 8 enterprise policy and providers |
| 9 Optional persistence scope/ownership | 2 |
| 10 Secret ownership | 2, 4 deletion, 6 recovery/rotation |
| 11 Quotas/fairness/shared resources | 5, 6 capacity gate |
| 12 Billing/entitlements | 5 usage/admission foundation; 7 payments/plans |
| 13 Tenant lifecycle/control plane | 1 runtime guard; 4 administration/deletion; 6 operations |
| 14 Unbounded queries/global scans | 5 |
| 15 Observability/incident controls | 5 instrumentation; 6 alerts/runbooks |
| 16 Shared backups/recovery | 4 retention/deletion model; 6 restore/recovery |
| 17 Tenant settings | 4 execution-relevant settings; 8 optional branding/domains |
| 18 Adversarial CI | 0 foundation; every phase adds coverage; 6 required release gate |

## Start with these implementation slices

1. Add the shared two-org fixtures and a real PostgreSQL/API/worker/frontend CI entry point, with a pinned cross-repo test manifest.
2. Fix and test the MCP descriptor leak plus ownership migration.
3. Fix and test outbound model destination enforcement.
4. Fix and test lifecycle eligibility across every execution entry point.
5. Fix and test browser teardown/draft ownership and interim tenant-aware login.

These first slices make the existing product safer and establish evidence for the larger account/storage migration. Account and schema changes then proceed through their compatibility gates without requiring a full rewrite or a database reset.
