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
- DONE Frontend dead-code removal, stale copy fix and the frontend handoff (frontend #27).
- DONE Backend handoff and this document (backend #35); data-model documentation (backend #36).
- DONE Legacy schema purge (backend #37, migration 22, deployed 2026-11-22): stale rows, indexes and the SSO trigger of removed flows deleted; rollback-reader machinery removed; dead-code pass. Verified on production: migration 22 recorded, no legacy rows, indexes or triggers left, healthy, no errors. Application images older than migration 22 can no longer be rolled back onto this database.
- DONE `docs/DATA_MODEL.md` and `docs/data-model/` (regenerated for schema version 22 with `scripts/export_data_model.py`).

### Explicitly out of scope for Phase 4
Time-limited operator support access (operators act only on the VM), public signup (stays off until Phase 6), deleted data in older backups (Phase 6).

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

## F. Known test flake
`T4-I04` (schedule editor control, `tenancy_harness/schedule_timezone_ui.cjs`) failed on 2 of ~4 frontend `main` runs and passed on re-run, with the report showing only `failure_locations` and no message. Diagnosability fix: the report now has `failure_messages` (per failing control: scrubbed, truncated Playwright error text plus a short console/network summary of the browser; secrets, bearer/JWT tokens, cookies and fixture header values are redacted; tests in `tests/tenancy/test_failure_messages.py`). Root cause: NOT reproduced as the CI failure. Repeating the control 20-100 times in one stack under CPU load (new local `--only-control T4-I04 --repeat-each N` option, dev-only) failed about 2-5% of runs, but those failures were confounded by repetition: fixed schedule names let the `expect.poll` for the London schedule pass on a schedule left by an earlier repetition, so the test moved on while the real Save was still in flight (a stale dialog then showed no inline error, a count assertion saw the late schedule, or Cancel waited forever). The same weakness could bite in CI whenever the dialog is slow to close under load: the test never waited for the dialog to finish saving and close before opening the next one. Hardening: unique per-run schedule names, explicit waits for the dialog to be open and idle and to be closed after each save, field values asserted after fill, assertions that tolerate unrelated schedules, explicit action timeouts, seeded zone restored at start. The `main` CI no longer runs the real-stack suite, so this no longer blocks deploys; a recurrence on a pull request will now print the real message. A Playwright run that dies with "Target page, context or browser has been closed" is usually machine memory pressure; rerun.

## G. Repository notes for the next engineer
- `/docs/` is listed in the backend `.gitignore` although it contains tracked files: add new documents with `git add -f`.
- Frontend `main` deploys only if its real-stack job passes; a failed job leaves production on the previous frontend while the backend may already be ahead. After any merge check that the deploy ran (`gh run list`) and that new files are served.

## H. State at handover (2026-11-22) and what is left

Tech ownership moves to Snigdh. Everything below is merged to `main` unless it says draft.

### Delivered since section A
- Phase 4 complete and deployed: organization settings and ownership transfer, schedule time zones, lifecycle (suspend, resume, deletion with grace period, export), legacy schema purge (migration 22).
- Sign-in page restored to the original split design (loom canvas, "Welcome back", one "Continue with Google", "New to Weaves? Onboard to Weaves"). "Onboard to Weaves" opens the chat Welcome step; the chat (name, path step "Where will you work?", organization, creating, first run) is one-time onboarding of a new organization.
- Onboarding backend (migration 23): account `role_title`; `is_owner` and `role_label` on every membership; organization `slug` (identifier only, no routing) with an availability endpoint; organization `email_domain` and the domain rule; creation records time zone and locale, returns request ids and `Retry-After`.
- CI: the slow real-stack job runs on pull requests only, not again on `main`; deploy needs `verify` green and real-stack passed or skipped. Flaky control T4-I04 hardened and its failures now print their real message.

- Waitlist backend (migration 24, branch `codex/waitlist-backend`, DRAFT PR): public `/api/v0/waitlist/*`, question sets as locked files in `contracts/waitlist`, row-policy `waitlist` purpose, operator script `infra/gcp/singlevm/waitlist.py`, retention tick, Caddy route for `weave-studio.in/api/v0/waitlist/*`, required control TW-01. The website flow (frontend) is not built yet; after merge: production check (health, migration 24 recorded, one test submission deleted with the operator script) and publish the privacy notice before the consent link goes live.
- Waitlist Other text (2026-11-26, branch `codex/waitlist-other-text`): question set 2 (new file, set 1 untouched and still served) sets `allow_other` on `role` and `company_location`; the server requires 1..60 trimmed characters whenever Other is chosen on an `allow_other` question. Operator export adds `<question>_other` columns and stats adds `other_texts`. Deploy only after the frontend 'Show a single Other option in waitlist questions' change is live.

- Removed the app sections "Approvals & Policy" and "Artifacts & Audit" (owner decision 2026-11-25; backend migration 31, frontend PR). Gone: `/approval-policies`, `/approvals`, `/approvals/{id}/decision`, the approval-policy and approval-request contracts, action recovery of approved writes, `pending_approvals` in `GET /api/v0/overview`, the `approval_policies.manage` permission and the data listed in `docs/DATA_MODEL.md` (migration 31). Kept: the audit trail (still written everywhere; `GET /api/v0/audit-events[/export]` stays for operators and tests), artifacts, and workflow approval steps (approve/reject in the workflow run view, permission `actions.approve`).

### Product decisions (owner), do not undo without asking
- Sign-in is Google only. No email codes, no passwords, no SMTP.
- NO invitation flow and NO "request access" flow. An admin adds a member's email in People; that person signs in with Google and is linked by verified email. No email is ever sent.
- An organization owns its custom email domain: a person whose verified email domain already belongs to an organization cannot create another organization at all (backend enforces, frontend hides every create entry). Public mailbox domains (Gmail, Outlook, Yahoo, ...) claim nothing and can still create organizations; this is intentionally open while the product is internal and must be tightened before public launch (`WEAVES_ENFORCE_ORGANIZATION_DOMAINS`, `WEAVES_PUBLIC_EMAIL_DOMAINS`).
- Sign-in is restricted by an allowlist on the VM (`WEAVES_ACCOUNT_ALLOWED_EMAILS`, optional `..._DOMAINS`). Anyone else sees "This account isn't allowed".
- Only the common UI parts are to be unified (dialog, notice/error box, toast, loader, transition); page designs are not redone.

### Left to do (in order)
1. Shared components (frontend DRAFT PR `codex/frontend-shared-components`): finish, run the full browser suite, fix failures, review. Start with `docs/handoff-specs/shared-components-STATUS.md` in the frontend repo.
2. Final onboarding pass against the design (`Weaves Onboarding.html`, extracts under `docs/handoff-specs/onboarding-design/` in the frontend repo), frontend only, using the shared components and the backend fields that already exist:
   - Creating step: the four progress lines ("Creating <org>", "Making you the Owner", "Preparing your workspace"; the slug line is optional), the "Your organization is ready" ready state, the "That didn't finish ... Retrying is safe" failure state with the request id from `X-Request-Id`, "Retry safely".
   - "Done" step: "N setup items are waiting for you on Home" and the woven transition "Opening your workspace as <role>..." (role from `role_label`).
   - Home: the "Finish setting up" card with all five tasks (key, agent, first task, teammates optional, tool optional) and progress; today Home still shows an older three-step card.
   - Banners: network ("Can't reach Weaves. Your answers are saved."), rate limit ("Too many requests ... try again in N seconds" from `Retry-After`), session expired.
   - "About you" step: "Role or team (optional)" using `PATCH /account/(browser/)me {role_title}`.
   - Organization step: show and edit the slug with the live availability check (`GET /account/(browser/)organization-slugs/{slug}`, "Available / Taken" and the suggestion), and the time zone and language fields; show the read-only organization URL.
   - Organization cards: role pill from `role_label`; hide "Manage deletion" from non-owners using `is_owner`; "Join <Org>" sub-line should show the role (it shows "Active" until the frontend reads `role_label`).
   - Step counter consistency in the reply card ("1 of 5" vs "2 of 6").
3. Review passes the owner planned: security, bugs, optimization (backend and frontend), then the architect ER/schema review (start at `docs/DATA_MODEL.md`).
4. Phases 5 to 8 (usage limits and fairness, backups and staging, plans and the payment gateway, enterprise SSO): see section C and `MULTI_TENANCY_IMPLEMENTATION_PLAN.md`.

### Open questions for the owner
- Is the owner's own organization allowed to keep Gmail-domain creation (today yes)? Decide before public launch.
- Payment gateway choice and pricing (Phase 7).

## I. Production facts learned during this work (read before touching the VM)
- Production is project `weaves-510819`, VM `weaves-personal`, zone `asia-south1-a` (IAP SSH only). Application settings live in the root-only `/opt/weaves/singlevm/runtime.env`.
- Values in `runtime.env` can be double-quoted (for example `WEAVES_ACCOUNT_ALLOWED_EMAILS="a@x.com,b@x.com"`). When editing, keep the quotes and put the whole comma list inside them; a malformed line breaks every `docker compose` command until fixed (this happened once and was repaired; `docker compose ... config -q` validates the file). Apply a change with `docker compose -p weaves-single --env-file /opt/weaves/singlevm/runtime.env -f /opt/weaves/singlevm/compose.yaml up -d product-api`.
- The allowlist now contains the owner and `snigdhchandra23@gmail.com`. A leftover file `/opt/weaves/singlevm/runtime.env.bak-allowlist` (a pre-repair copy with a BROKEN allowlist line) and `/tmp/lc.py` (a copy of the organization lifecycle operator script) are on the VM; delete them.
- On 2026-11-22 the owner's account rows (the account, its two identities and its membership; not the tenant user or organization) were deleted by hand with SQL to retest sign-in; no audit event was written for that. A later sign-in re-links the same email to the existing tenant user in "Weaves Personal" automatically (verified-email linking), so it lands in the app, not in the onboarding chat. To test the chat as a brand-new person use the Onboard path with an account that has no tenant user, or "Create a new organization" from the path step.
- Migrations are forward-only; application images older than migration 22 cannot be rolled back onto this database. Head is 23.
- Merging to `main` deploys to production for both repositories. The real-stack suite runs on pull requests only: wait for the PR to be green.
- Frontend CI `deploy` only runs when `verify` passes; a red real-stack job on a pull request means do not merge.
