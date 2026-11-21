# Weaves: status and backlog

Status date: 2026-11-22. Companion to `docs/HANDOFF.md` (context) and
`MULTI_TENANCY_IMPLEMENTATION_PLAN.md` (authoritative plan, one progress paragraph per slice).
Legend: DONE = merged to `main` and deployed; IN REVIEW = open PR; OPEN = not started.

## A. Done (Phases 0 to 4)

### Phase 0: test foundation
- DONE Real-stack harness: PostgreSQL, API, native worker, Chromium, fake model/connector/Google providers.
- DONE Ownership inventory (`tenancy_harness/ownership.json`) enforced by tests; required-controls list in `pair.json`; cross-repo pinning and byte-for-byte vendoring of the harness.
- DONE Opt-in live Anthropic stage through a budget-gated relay (USD 100 lifetime budget).

### Phase 1: contain confirmed leaks
- DONE Connector descriptor isolation; model-provider egress containment (DNS pinning, redirects, proxies).
- DONE Lifecycle guard: suspended organizations and archived workspaces stop new work everywhere.
- DONE Browser teardown on organization or session change, scope generations, namespaced drafts.

### Phase 2: ownership below HTTP
- DONE Mandatory tenant context (fails closed in durable processes); immutable record ownership.
- DONE Tenant-owned encrypted secrets; cross-tenant reference validation; tenant-keyed client IDs.
- DONE PostgreSQL row-level security with a restricted role and named system scopes, enabled in production.

### Phase 3: accounts and sign-in
- DONE Global accounts and memberships; per-request organization selection; cookie sessions with CSRF.
- DONE Google OpenID Connect as the only sign-in (backend PRs #27, #28; frontend PR #23). All legacy flows removed (passwords, email codes, SMTP, invitations, tenant SSO, token signup).
- DONE Onboarding conversation and real first-run checklist (frontend #23). Production cutover completed; sign-in restricted to the owner's allowlist.
- DONE Operator scripts for account bootstrap and break-glass sessions.

### Phase 4: onboarding and tenant lifecycle
| Slice | Backend | Frontend | Real-browser control |
|---|---|---|---|
| Account organization creation (idempotent, capped) | earlier PRs | #23 | T1-I11 |
| Organization settings, explicit owner, ownership transfer, member removal, concurrency-safe administration | #30 | #24 | T4-I01 |
| Schedule timezones with DST rules, legacy zone backfill script | #31 | #25 | T4-I03, T4-I04 (#33) |
| Lifecycle: suspend/resume (operator), deletion with grace period, resumable purge, tenant export | #32 | #26 | T4-I02, T4-I05 (backend PR after #33) |
- DONE Verification per slice: unit, PostgreSQL (restricted role), real-stack controls and the live Anthropic stage.
- DONE Production checks: healthy deploys, row-level security active, legacy login routes gone, a restore of a fresh backup matched row for row (2026-11-21).

### Finishing work for Phase 4
- IN REVIEW Frontend dead-code removal and copy fix, plus the frontend handoff (frontend #27).
- IN REVIEW Backend handoff and this document (backend #35).
- IN PROGRESS Legacy schema purge: a forward-only migration deleting the stale rows, indexes and the SSO trigger left by removed flows, removal of rollback-reader machinery, a dead-code pass.
- IN PROGRESS `docs/DATA_MODEL.md`: collection catalogue, ER diagrams, physical schema and review questions.

### Explicitly out of scope for Phase 4
Time-limited operator support access (operators act only on the VM), public signup (stays off until Phase 6), deleted data in older backups (Phase 6), and owner-only visibility of lifecycle actions in the chooser.

## B. Owner decisions in force
- Internal-only product; data not valuable; breakage acceptable; no backups required and no rollback compatibility for now.
- Google sign-in only; no legacy support.
- Owner will run the later reviews: security/bugs/optimization, then an architect ER and schema review.
- Phases 5 to 8 and the payment gateway are handed to another engineer; tech development and testing continue with them.

## C. To do after Phase 4

### Reviews (owner, before or alongside Phase 5)
1. Security review: sign-in and session code (`modules/auth/google_*`, `apps/api/authentication.py`), row-level security policies and the named system scopes, secrets handling, egress containment, operator scripts, export content.
2. Bug and correctness review of Phase 4 code, in particular purge, ownership transfer and schedule time math.
3. Optimization review, backend and frontend (see hot-path notes in `HANDOFF.md` section 5 and `DATA_MODEL.md` once merged).
4. ER and database schema review by an architect; start from `DATA_MODEL.md`.

### Phase 5: usage limits, fair execution, scale
- OPEN Define limits and enforce them atomically: requests, seats, queued and running work, tool calls, storage, schedules, usage and spend; global emergency limits.
- OPEN Fair claiming of queued work across tenants (the claim query is oldest-first across all tenants today); per-tenant concurrency caps.
- OPEN Tenant usage accounting and idempotent reservations that survive crashes; separate customer-supplied keys from platform-paid usage.
- OPEN Bound unbounded scans and histories; aggregate dashboard metrics server-side; due-record queries instead of full scans.
- OPEN Structured per-tenant observability and a declared capacity envelope; noisy-neighbor tests.

### Phase 6: recovery and release readiness
- OPEN Off-host, encrypted, tested backups with tenant-selective restore and deletion tombstones (so restore cannot resurrect deleted tenants).
- OPEN Staging, rollback rehearsal against real old images, alerting, tenant kill switch, runbooks, supported capacity.
- OPEN Gate for public signup (stays off until this passes).

### Phase 7: plans, subscriptions, payment gateway
- OPEN Choose the gateway (not chosen). Plans and entitlements, subscription lifecycle, invoices, signed idempotent webhooks, proration and grace rules, reconciliation, billing UI.
- OPEN Non-payment handled through the existing organization lifecycle; integer minor-unit amounts with explicit currency; sandbox tests for duplicate, delayed, reordered, forged and replayed events.

### Phase 8: enterprise identity
- OPEN Organization-bound OIDC/SSO, verified domains, SSO-only policy on every session path, session policy revocation, optional MFA and SCIM.

### Known small items (any time)
- Owner flag on memberships so the chooser hides deletion actions from non-owners.
- Read endpoint exposing the organization timezone to non-admins.
- Show a person's name instead of "owner" or "operator" in lifecycle views.
- Persist the recovery screen across a reload.
- Rate-limit the Google sign-in start route.
- Split `dashboard.js` (about 3.7k lines) by page.
- Decide whether to remove the implicit development identity in memory-only mode (about 80 tests use it).
- Re-pin the harness to merged commits after each squash merge (frontend merges change the commit).

## E. Who does what: AI work and human work

Rule of thumb: AI-assisted engineering builds, tests and documents; humans decide, approve risk, hold
credentials and judge what only they can judge. Anything that spends money, touches production data
irreversibly or changes who may access the system needs a human yes.

### E1. AI-doable work (build, test, document; each slice still gets unit, PostgreSQL, real-stack and live AI runs)
| Area | Work |
|---|---|
| Phase 5 | Limits and admission control, fair queue claiming, usage ledger and reservations, bounded queries, per-tenant metrics, noisy-neighbor and reservation-recovery tests |
| Phase 6 | Backup automation scripts and restore drills, tenant-selective restore, deletion tombstones, staging definition as code, rollback rehearsal tooling, alert and kill-switch implementation, runbooks |
| Phase 7 | Gateway integration code, plans and entitlements, subscription state machine, signed idempotent webhook inbox, reconciliation jobs, billing UI, sandbox-based test suite (once a gateway is chosen) |
| Phase 8 | Organization-bound OIDC, verified domains, SSO policy enforcement, session revocation, optional MFA/SCIM, full policy-matrix tests |
| Quality | Fixes from the reviews below, backend and frontend optimization, splitting `dashboard.js`, the small items in section C, keeping the harness pins and docs current |
| Docs | Keep `HANDOFF.md`, `DATA_MODEL.md`, this file and the plan current after every slice |

### E2. Human work
| Area | Work |
|---|---|
| Approvals | Merge or approve high-risk changes (migrations, anything touching production data or sign-in); decide when to flip public signup on |
| Reviews | Security review, bug review, optimization review, and the architect ER and schema review (AI can prepare and pre-screen; a person signs off) |
| Decisions | Choose the payment gateway and commercial model (plans, pricing, currency, tax, refunds, grace periods); define supported capacity and limits per plan; decide data retention and legal terms |
| Credentials and accounts | Create and own provider accounts (payment gateway, Google OAuth production consent screen, domains, DNS, email if ever needed); rotate and store secrets |
| Production | Authorize destructive production actions (running the legacy purge or deleting real tenants), restore drills against real data, budget for paid AI testing beyond the USD 100 ledger |
| Product | Real Google sign-in walkthrough, UX and copy judgment, onboarding design feedback, new use cases |
| Compliance | Privacy policy, terms, data-processing agreements, payment compliance (PCI scope is the gateway's, but contracts and tax are yours) |

### E3. Suggested order
1. Merge the open PRs (schema purge, data-model doc, handoffs, frontend cleanup).
2. Human reviews (security, bugs, optimization, ER) with AI preparing evidence; AI fixes findings.
3. Phase 5 (AI), then Phase 6 (AI plus human approvals), in parallel with the gateway decision (human).
4. Phase 7 then Phase 8 (AI), each released only after its test matrix passes.

## D. Where to continue
Start with `docs/HANDOFF.md`, then `MULTI_TENANCY_IMPLEMENTATION_PLAN.md` (Phases 5 to 8 and the shared testing matrix), `docs/DATA_MODEL.md`, `infra/gcp/singlevm/README.md` and the frontend `docs/HANDOFF.md`.
