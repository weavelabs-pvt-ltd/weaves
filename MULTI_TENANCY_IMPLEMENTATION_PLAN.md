# Multi-tenancy implementation and release plan

Planning baseline: 2026-11-20. Based on [the tenancy audit](MULTI_TENANCY_AUDIT.md), backend `d70176dfbe3c328e6e3070fa2f79f5c123a23db2`, and frontend `6f6ed9888b398921e2d567e0671b954df64185e8`.

Status: Phases 0–2 are implemented and released. Phase 3 account identity, memberships, per-request organization selection, cookie/CSRF sessions and the Google-only sign-in implementation are complete; the current release finishes production cutover and onboarding verification. Phase 4 organization creation and the conversational onboarding/checklist are implemented, and organization settings, explicit ownership transfer, membership removal and administration concurrency are implemented in the current slice; organization lifecycle, export and resumable deletion are implemented in the next slice, while schedule-timezone lifecycle remains open. Phases 5–8 are not complete. Public self-service access remains off; production Google sign-in is restricted to the owner's explicit email allowlist.

Current decision (2026-11-21): Google is the only human sign-in method. Passwords, email-code login, invitations, tenant SSO and token-gated legacy onboarding are removed. The historical checkpoint paragraphs below describe their implementation sequence, not instructions to restore those flows. Service accounts and tenant API credentials remain supported.

Google cutover validation covers the restricted PostgreSQL role, real Chromium account/organization switching and logout, organization creation, plus a budget-gated real Anthropic task/approval workflow. Deployment uses the same-origin app API proxy, forwards the organization-creation flag only when explicitly enabled, and links the existing owner user/principal before bootstrap decides whether a new organization is necessary. Production credentials stay in the root-only runtime environment. The user must complete Google's interactive sign-in to verify the external consent/identity leg of the live flow; operator recovery sessions are tested separately and never presented as a Google login.

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
### Phase 3 progress: account data model (slice 1)

Adds the `accounts`, `account_identities` and `memberships` collections (global account, verified login identity, account-to-organization membership that references the existing tenant user and principal), with no behavior change. Uniqueness is database-enforced by idempotent indexes applied at startup: a verified identity belongs to one account platform-wide, an account never holds the same identity twice, one membership per account and organization, and one account per tenant-local user. Row policies treat accounts and identities as system-only and memberships as organization-scoped; a new `account` system scope allows the cross-organization lookups the organization chooser will need. The policy functions are now re-applied at every start, so policy changes no longer need a schema version. `plan_account_backfill` classifies legacy users (single organization, shared across organizations, duplicate within an organization, skipped) without writing or merging anything; counts only are printable. Next slices: account email-code login and sessions, per-request organization selection, the verified linking flow, then the frontend chooser.

### Phase 3 progress: account sign-in (slice 2)

Adds account-level email-code sign-in and account sessions, with no tenant resource access. `POST /api/v0/account/email-code` always returns an opaque challenge id and emails a 10-minute, five-attempt HMAC-protected code to any address, so the response never reveals whether an account or membership exists; throttles are global (`account-email-code` per address, `account-email-source` per source address) and return 429 with `Retry-After`. `POST /api/v0/account/email-verify` consumes the challenge once, finds or creates the Account and a verified email identity (the existing unique index decides races), then links memberships: each ACTIVE or INVITED tenant user whose normalized email equals the verified address, with a user-type principal and an existing organization, gets an ACTIVE membership unless one already exists, so removed or suspended memberships are never resurrected and no other address or attribute links anything. Disabled accounts cannot sign in or keep using a session. Sessions are opaque `wvs_acct_` bearer tokens stored only as sha256 digests (new unique index) with a 12-hour expiry; `GET /api/v0/account/me` returns the account, masked verified identities and active or suspended memberships, and `POST /api/v0/account/logout` revokes the session. Middleware authenticates `/api/v0/account/*` with account sessions only and binds no tenant identity; tenant tokens fail there and account tokens fail on every tenant route. The new `account_login_challenges` and `account_sessions` collections are system-only (readable under the `account` and `auth` scopes), and the PostgreSQL tests run under the restricted `weaves_app` role. Not yet done: browser (cookie/CSRF) account sessions, audit events for sign-in and linking (the `account` scope does not cover `audit_events`), per-request organization selection, and cleanup of expired challenges and sessions. Next slices: per-request organization selection, the verified linking flow for legacy credentials, then the frontend chooser.

### Phase 3 progress: per-request organization selection (slice 3)

An account session (`wvs_acct_` bearer) can call tenant routes by naming one organization in `X-Organization-Id` on every request; there is no mutable "current organization", so two tabs can work in different organizations under one account. The middleware re-derives access from current state each time: an ACTIVE membership, an ACTIVE tenant user, an ACTIVE user principal that belongs to that user, and an ACTIVE organization, then the existing workspace resolution. The request then runs as the tenant-local principal, so roles, audit attribution and ownership are unchanged. A missing header is 401; an unknown organization, a missing membership, a removed or suspended membership, a disabled user or principal and a suspended organization all return the same 403 body, so nothing about other organizations is disclosed, and one organization's change never affects another. Tenant tokens, API tokens and cookies ignore the header, and account sessions never reach tenant routes without it. `T1-I08` is a required real-stack control. Invited-but-not-yet-active users cannot select an organization through an account until they are activated; that arrives with the invitation flow in Phase 4.

### Phase 3 progress: account browser sessions (slice 4)

The browser can hold an account session in an HttpOnly cookie, mirroring the legacy tenant browser design without changing it. `POST /api/v0/account/browser/email-verify` (same body as the bearer verify) requires the configured same-origin Origin, issues a `channel: "browser"` account session with its own `wvs_acbr_` token prefix, and sets `__Host-weaves_account_session` (Secure, HttpOnly, SameSite=Strict, path `/`, 12-hour absolute lifetime like the legacy browser session, no sliding extension). The body returns the account, memberships and a session-bound CSRF token but never the credential. Each successful verify revokes the account browser session the browser presented and issues a new one; `GET /api/v0/account/browser/me` and `POST /api/v0/account/browser/logout` (revokes and clears the cookie) are cookie-only. The middleware accepts the account cookie on `/api/v0/account/browser/*` and, with `X-Organization-Id`, on tenant routes using exactly the bearer selection rules and the same 403 refusal; unsafe methods need the `X-Weaves-CSRF` header plus an allowed Origin, safe methods do not. Channel isolation is enforced by prefix and by the stored channel: a browser token fails as a bearer, a bearer token fails as the cookie, an account cookie never authenticates legacy `/auth/browser/*` routes and the legacy cookie never authenticates account routes. If both cookies are present the legacy cookie keeps precedence on tenant routes. Revocation, membership changes and account disablement apply on the next request. Origin, CSRF and cookie helpers are shared with the legacy flow (`request_refusal`, `set_session_cookie`); legacy behavior and tests are unchanged. Next: the verified linking flow for legacy credentials and the frontend chooser.
### Phase 3 progress: verified email linking (slice 5)

A signed-in account can attach another email only after proving control of that mailbox: `POST /api/v0/account/identities/email-code` sends a code bound to the calling account (it can never sign anyone in, and a sign-in code can never attach an email), and `email-verify` creates the verified identity and links any tenant users carrying that address. An address already verified on a different account is refused with a conflict and nothing moves; merging accounts is deliberately not available through this flow. `DELETE /api/v0/account/identities/{id}` removes an identity but never the last verified one, and only the owner can remove it. `/account/me` now returns identity ids. Tenant credentials stay tenant-bound, and a claimed or unverified address never links anything. `T1-I09` is a required real-stack control.

### Phase 3 progress: account hygiene (slice 6)

Three weaknesses of the account layer are removed without changing any external behavior. Account code no longer scans whole collections: the repository port gains `list_by_field` (exact or case-insensitive payload match, ordered by identifier) and `list_before` (bounded, timestamp-ordered), implemented in memory and PostgreSQL, and identity, membership, session, challenge and tenant-user-email lookups use them with new idempotent index-only partial/expression indexes in `account_schema.py` (an older release can still roll back); tests assert via `EXPLAIN` that each query avoids a sequential scan. A bounded purge (`AccountSignInService.purge_expired`, at most 200 rows per call, under the `account` system scope) deletes login challenges and sessions one hour after they expire or are revoked; the worker calls it from its existing 30-second recovery tick, never from a request path, and a failure is logged without disturbing recovery. Finally, when linking creates a membership in organization X, `account.membership.linked` is written to X's own audit trail inside the same unit of work as the membership (actor is the tenant-local principal, metadata carries only the account id, never the email) under a request identity for X, so row-level security admits it without widening the `account` system scope; a failed audit write rolls the membership back. Identity attach and remove write no tenant-visible event (identities are global) and log a secret-free line instead.

### Phase 3 progress: browser account journey control (T1-I10)

The required real-stack suite now drives the frontend account journey in Chromium against the real API: account sign-in with an emailed one-time code read from the SMTP fixture, the organization chooser for the person who belongs to both fixture organizations, entering one organization, and switching to the other through the shell. It asserts every tenant request carries the selected `X-Organization-Id`, content from the previous organization is gone after the switch, and the new organization's data is shown. The pinned frontend revision is the account-journey change under review in the frontend repository; it is re-pinned to the merged frontend commit once that lands.

### Phase 4 progress: account organization creation (slice 1)

A verified account can create an organization with `POST /api/v0/account/organizations` (account bearer session) or `POST /api/v0/account/browser/organizations` (account cookie plus CSRF). Both are off unless `WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION` is true (otherwise 404), require an `Idempotency-Key` (1-128 characters), and return the organization, default workspace and the creator's membership with no bearer token or API credential: the creator then acts with the account session and `X-Organization-Id`. The owner email must be one of the caller's verified email identities (default: the earliest verified one). Provisioning is one unit of work: organization, workspace, owner user and USER principal, Admin/Builder/User roles and the Admin binding, default provider/profile/approval policy, an ACTIVE membership, the `organization.created` audit event (actor is the owner principal; the email is not recorded) and an `account_organization_requests` idempotency record. The legacy token-gated `/onboarding/organizations` flow shares the same provisioner (the API credential is now optional in `_build_owner_records`) and is unchanged. No new system purpose was needed: the store re-applies the restricted role and the `app.org_id`/`app.system_purpose` settings on every repository call inside a transaction, so tenant rows are written under the new organization's own request identity while the idempotency record and account reads run under the existing narrow `account` scope, all in one transaction. `account_organization_requests` is a global account collection (RLS-gated to the `account` scope, unique index on account and key digest, deterministic id from account and key digest); the same key and body replays the same organization (200), the same key with a different body is 409, keys are per account, concurrent identical requests create exactly one organization (per-account advisory lock plus the unique key as a backstop), a per-account cap (`WEAVES_ACCOUNT_ORGANIZATION_LIMIT`, default 3, counted from the idempotency records) returns 403, and attempts are throttled per account and per source address. Failure injection after each of the 14 provisioning writes proves no partial tenant remains and a retry with the same key succeeds, on memory repositories and on PostgreSQL under the restricted role. `timezone` and `locale` are validated and fingerprinted but only recorded in the audit metadata until the organization settings model exists.

### Legacy sign-in removed

The account flow is now the only way a person authenticates. Removed without replacement or compatibility paths: tenant password login, tenant email-code login, password enrollment and logout (`/auth/login`, `/auth/email-code`, `/auth/email-verify`, `PUT /auth/password`, `/auth/logout`), the legacy tenant browser cookie routes (`/auth/browser/*`), organization SSO (`/auth/sso/*`, the SSO connection and external-identity services and their documents), the public `POST /onboarding/organizations` signup with `WEAVES_ALLOW_ORGANIZATION_ONBOARDING` and `WEAVES_ORGANIZATION_ONBOARDING_TOKEN`, the `UserSession`, `UserPasswordCredential`, `EmailLoginChallenge`, `SsoConnection` and `SsoExternalIdentity` contracts, repositories and services, password hashing, and tenant login throttling and its per-organization bucket scope. No migration deletes the old rows: collections nothing reads any more simply keep their leftover rows, and the old indexes and the SSO identity immutability trigger remain in the schema (startup and schema-version checks do not depend on them; the `auth` row-policy scope no longer lists the removed collections). Account-level OIDC is a later phase and nothing is stubbed for it. The middleware accepts exactly four credentials and answers 401 to everything else: an account bearer session with `X-Organization-Id`, an account browser cookie with CSRF and `X-Organization-Id`, a service API credential, and the static `WEAVES_API_TOKEN(S)` development tokens (memory-only local runs with no token configured keep the implicit local developer). `GET /auth/me`, service accounts, credentials and the admin `/users` routes are unchanged; `/users` now reports whether each member has linked an account instead of a tenant sign-in history, and suspension takes effect on the next request because every account request re-derives its membership, user and principal state.

Admin-provisioned members: first verified account sign-in activates an INVITED user whose user-principal is DISABLED (user and principal become ACTIVE, with a `user.activated_by_email` audit event) in the same unit of work as the membership link; suspended and deleted users and their principals are never touched, so a person with an INVITED user can select the organization immediately after first sign-in. With no email delivery on the production VM, two operator-only scripts replace the password bootstrap and run on the VM with database owner access (`app_role=''`) and are never reachable over HTTP: `bootstrap_account_owner.py --email E --organization-name N` (idempotent: Account, verified email identity, organization through `provision_organization` with an ACTIVE owner and no API credential, and the membership) and `issue_account_session.py --email E [--minutes 60]` (break-glass account bearer session for an existing active account, token printed once on stdout, refuses unknown or disabled accounts). Both write one structured, secret-free log line. The real-stack tenancy harness still drives the removed routes until its fixtures are rewritten; it is expected to fail until then.
### Harness is account-only

The real-stack tenancy harness no longer uses any legacy sign-in flow, so it runs unchanged before and after the legacy flows are removed from the backend. Every fixture organization is created by an owner account that proves its email through the SMTP fixture (`/account/email-code`, `/account/email-verify`) and calls `POST /account/organizations` with an `Idempotency-Key`; members (Builder, User, the shared person across both organizations) are provisioned with `POST /users` and sign in through the account flow, so an actor is an account bearer plus `X-Organization-Id` and `X-Workspace-ID`. Service accounts keep their API credential. Browser controls seed the SPA's `weaves.account`, `weaves.organization` and `weaves.workspace` session keys. The controls whose subject was a removed flow (`T0-I02` browser tenant sign-in, `T1-I06` tenant-scoped sign-in throttles) are deleted; the account controls `T1-I07` to `T1-I11`, lifecycle `T1-I04` and the teardown control `T1-I05` (now an account session that is signed out, revoked server-side and replaced by another account's session) stay required.

### Google sign-in replaces email codes

The owner decided that Google OpenID Connect is the only sign-in method, with no email codes, SMTP or compatibility paths. Removed: `POST /account/email-code`, `/account/email-verify`, `/account/browser/email-verify`, `/account/identities/email-code` and `/account/identities/email-verify`, the `account_login_challenges` collection (contract, repository, row policy, ownership and indexes), the per-address and per-source code throttles, second-email linking by code, and the SMTP/console email sender with its settings. Added: `GET /api/v0/account/google/start` and `/callback` (public) using the authorization-code flow with PKCE S256, state and nonce. Each attempt is a 10-minute, single-use `account_oauth_states` row (global_account; only digests of the state and of the browser-binding cookie are stored) bound to the browser by a `__Host-weaves_google_oauth` cookie. The callback consumes the row exactly once, exchanges the code with the client secret and verifier, and verifies the ID token with PyJWT (RS256 against the cached provider JWKS with kid-rotation refresh, issuer from the discovery document, audience, exp/iat with 60 seconds of skew, nonce, `sub`, verified email). The account resolves by the verified OIDC identity first, then by an existing verified email identity (so the operator-bootstrapped owner lands in the existing account and organization), else a new account is created; both a verified OIDC and a verified email identity are then ensured, and an OIDC subject and email owned by different accounts is refused as `conflict` without changes. Invited members are linked and activated exactly as before. Every failure redirects to `/#account-signin?error=<access_denied|state|not_allowed|conflict|disabled|unavailable>`. `WEAVES_ACCOUNT_ALLOWED_EMAILS` / `WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS` restrict who may sign in or be created; with neither set any verified Google email is accepted. `PATCH /account/me` and `/account/browser/me` edit the display name, and CORS allows `X-Organization-Id` and `X-Weaves-CSRF`, exposes `Retry-After` and no longer lists the retired onboarding header.

### Harness signs in through a fake Google provider

The real-stack harness signs in only through Google. Its existing `provider` fake now also serves a fake OpenID Connect provider (discovery, JWKS, authorize without a consent screen, and a token endpoint that validates the code, redirect URI, client credentials and PKCE verifier before returning an RS256 ID token). The API reads discovery and token/JWKS URLs on the private network, while discovery's `authorization_endpoint` is the harness reverse proxy so Chromium can reach it. Fixtures drive the same start/authorize/callback redirects over HTTP and keep the session cookie plus CSRF token; browser tests choose the person through a private control file and follow the real redirect chain. The SMTP listener, mailbox and email-code fixtures are gone. T1-I07 to T1-I09 and T1-I11 were rewritten for Google (allowlist, unverified email and provider denial create nothing; identity removal keeps the last verified identity). The signed-in browser controls depend on the onboarding frontend and are isolated behind `tenancy_harness/browser_account.cjs`.

### Legacy schema purge

Product owner decisions: the product is internal-only, its data is not valuable, no backups or reader-rollback compatibility are required, and legacy sign-in is gone from the code. Migration 22 (`infrastructure/database/legacy_purge.py`, one transaction, appended after migration 21) therefore removes what the retired concepts left in an existing database:

- Rows: every `product_records` row whose collection is in `PURGED_COLLECTIONS`: `user_passwords`, `user_password_credentials`, `user_sessions`, `user_invitations`, `sso_connections`, `sso_external_identities`, `email_login_challenges`, `account_login_challenges`, `memory_items` and `knowledge_documents` (the last two belong to features removed earlier).
- Indexes (`DROP INDEX IF EXISTS`): `product_records_user_session_digest_uq`, `product_records_user_invitation_digest_uq`, `sso_identity_subject_uq`, `sso_identity_user_connection_uq`, `sso_connections_org_provider_idx`, `account_challenge_email_idx`, `account_challenge_expiry_idx`.
- The `sso_identity_immutable` trigger and the `reject_sso_identity_rewrite()` function.
- Kept on purpose: `product_records`, `product_schema_migrations`, `product_login_throttle` (organization creation is still throttled through it), the secrets and catalog ownership tables, and `user_email_lower_idx` (verified-email account linking still queries `lower(users.email)`).

Migration history rule (documented in `store.py`): migrations are append-only and forward-only. Migrations 1-21 stay as released; a new database replays them and then migration 22 so it converges with an old one. The reader-rollback tolerance (`_READ_COMPATIBLE_MIGRATIONS`) and every hard-coded version range are gone: `SCHEMA_HEAD_VERSION` and `KNOWN_SCHEMA_VERSIONS` derive from the migration list, and the catalog ownership activation derives its prerequisites from it. Version 20 is the one operator-activated step recorded outside the list. A database that records a version this code does not know is still rejected at startup. There is no automatic downgrade; roll forward.

Drift protection: `tests/integration/test_collection_drift.py` builds the rich two-tenant world on PostgreSQL and fails if any stored collection is not in `REPOSITORY_SPECS`, and fails if SQL in the code names a collection that is neither registered nor in `PURGED_COLLECTIONS`; `tests/integration/test_legacy_purge.py` migrates a legacy-shaped database (rows in every purged collection, all old indexes and the trigger present) and proves legacy rows and objects are gone, kept rows are byte-identical, row policies still isolate tenants under `weaves_app`, the migration is idempotent and safe on an empty or current database, and every index, table and trigger of the final schema is justified. Dead code removed in the same change: `CapabilityProvider` protocols, an unused OAuth helper, an unused route parameter, and the removed collections in the organization purge order.

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

### Phase 4 progress: organization settings and safe administration

Organization preferences and explicit ownership live in a separate tenant-owned
`organization_settings` record, preserving the prior `Organization` payload for
reader rollback. New provisioning writes it atomically. Legacy reads derive the
owner only from an unambiguous creation audit and active human administrator;
they never choose an arbitrary current Admin or write on GET. Missing/ambiguous
proof requires operator recovery. Existing preferences use creation-audit values,
with `Asia/Kolkata` / `en` defaults for older organizations.

The bottom profile menu opens the shared settings dialog for organization-scoped
administrators. Name, IANA timezone and locale edits require the observed
revision; stale forms receive 409 and keep their draft until explicitly reloaded.
These are organization preferences: this slice does not rewrite existing workflow
schedule timing or implement the remaining timezone/DST scheduling work.

Only an account-authenticated current owner can transfer to another active,
already-linked human member. The recipient receives org Admin; the former owner
remains Admin. Confirmation names the organization, and the settings revision
prevents competing transfers. Role grants/replacement/deletion, member
suspension/reactivation, settings writes, ownership transfer and membership
removal share one per-organization PostgreSQL transaction lock. Authorization is
rechecked after acquiring it. The owner cannot be suspended, removed or stripped
of administration before transfer; inactive identities never count as surviving
administrators. Removal revokes tenant credentials and the one membership, while
retaining the global account, other memberships and existing work history.

Validation includes two independent API runtimes on restricted PostgreSQL,
concurrent settings/transfer/revocation cases, authorization revoked while waiting,
audit-failure rollback and foreign-tenant denials. Required real-stack control
`T4-I01` drives the actual settings dialog, two-way ownership transfer and People
membership removal, then proves the same account still accesses its other tenant.
This is one Phase 4 slice, not full lifecycle completion or SaaS readiness.

### Phase 4 progress: schedule timezones

Every schedule and calendar record now carries an explicit IANA `timezone`
(additive optional field, max 64 characters, validated with `zoneinfo`; unknown
zones are 422). A record without one is legacy and is read as `Asia/Kolkata` at
read time by the one shared module `modules/workflows/schedule_time.py`; nothing
is rewritten at startup, so production correctness never depends on the
operator script `infra/gcp/singlevm/backfill_schedule_timezones.py` (dry run by
default, `--apply` writes `Asia/Kolkata` only into records lacking a zone, keeps
`updated_at`/`next_run_at`, idempotent). New records use the request's zone, else
the organization's `organization_settings.timezone`, else `Asia/Kolkata`. All
instants are stored and sent as timezone-aware UTC; read models return the
effective `timezone`.

Calendar rules (hourly, daily, weekly at a wall-clock time) are evaluated in
the record's zone. A nonexistent local time (spring-forward gap) runs at the
first valid instant after the gap (02:30 becomes 03:00). An ambiguous local time
(fall-back overlap) runs once, at its first occurrence. `hourly` fires at every
real instant whose local minute is zero. There is no monthly rule today, so no
month-end clamp exists. Occurrences are strictly increasing and never repeat.
Existing `Asia/Kolkata` values are identical to the previous implementation
(golden values captured before the change). Interval schedules
(`interval_seconds`) are elapsed-time based and timezone independent; their math
is untouched and their zone is metadata.

Timezone-change rule: naming a different zone requires the observed
`expected_updated_at` (422 if absent, 409 if stale) and keeps the pending UTC
instant unchanged; the new zone applies to the occurrence after it, so nothing
fires early, twice or shifts silently. If the same edit changes the calendar
(frequency, days or time of day), or turns the schedule on, the pending
occurrence is recomputed from now in the new zone. Unrelated edits keep the
pending occurrence. Validation adds unit, DST, property, golden, API,
PostgreSQL restricted-role concurrency and backfill tests, and required
real-stack control `T4-I03`.

### Phase 4 progress: onboarding design parity

Additive backend support for the onboarding screens, with no invitations, access requests, email or new sign-in methods. `Account.role_title` (PATCH `/account/me` and `/account/browser/me` accept `display_name` and/or `role_title`); each membership on the account views gains `is_owner`, `role_label` and `organization_slug`; organization creation persists the submitted IANA timezone and locale, accepts an optional `slug`, claims the creator's custom email domain, and returns all of them. The account views gain `organization_creation` (allowed, or why not). A person whose verified email domain is already claimed by an organization cannot create another one (409 `domain_belongs_to_organization`); public mailbox providers claim nothing. Slug availability is `GET /account/organization-slugs/{slug}` (and the `/account/browser` variant), throttled like creation and naming no organization. Error responses carry `X-Request-Id` (exposed through CORS), and throttling answers 429 with `Retry-After`. Migration 23 adds the settings fields' backfills and two partial unique indexes; failure-injection, race, backfill and restricted-role PostgreSQL tests cover it, and the account control in the real-stack suite asserts the new fields (the fixture domain `tenancy.test` is declared public; `custom-claim.test` exercises the domain rule).

### Phase 4 progress: schedule timezone editor control (T4-I04)

The required real-stack suite now drives the actual schedule editor in Chromium: interval schedules show their own zone (a legacy record reads as Asia/Kolkata), a zone-only edit sends the loaded revision and leaves the pending UTC instant unchanged, a new schedule can name its own zone, and an unknown zone is refused by the server, shown inline and creates nothing. The harness pins the frontend change under review and is re-pinned to the merged frontend commit once it lands.

### Phase 4 progress: lifecycle screens control (T4-I05)

The required real-stack suite drives the lifecycle screens in Chromium: one signed-in person with three organizations sees a chooser where the operator-suspended one is shown, explained and not enterable; the owner exports an organization (a JSON download named for it), requests deletion by typing the exact name, and every route except the recovery ones is refused while it is pending; cancelling from the recovery screen returns the organization to service and the other organization is untouched. The suspension is performed by the real operator script. The harness pins the lifecycle frontend change under review and is re-pinned to the merged frontend commit afterwards.

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

**Exit gate:** one identity can reliably use multiple organizations with independent membership and permissions. The Google-only account journey replaces all legacy human sign-in paths; no compatibility login remains, following the internal-product decision.

### Phase 4 progress: public waitlist backend (migration 24, TW-01)

The website's "Sign in" links are being replaced by a waitlist that only collects data. The backend adds five public, unauthenticated routes under `/api/v0/waitlist` (questions, join, resume, save and clear one answer) that never touch accounts, organizations, users, sessions or the allowlist, and are deliberately outside cookies and CSRF (a submission is reached only by its private `X-Waitlist-Token`, stored as a SHA-256 digest, 30 days). Two global collections (`waitlist_entries`, one per email with a derived id and a unique `lower(email)` index; `waitlist_submissions`, one per join) are hidden from every tenant and every system purpose except a new `waitlist` purpose by the row policy (the earlier null-org pass-through is closed for them), registered in the ownership inventory, the data-model docs and the drift tests, and excluded from organization export and purge. Question sets and consent texts are immutable, lock-pinned files in `contracts/waitlist`. Abuse limits reuse the shared database limiter (join 10/hour per source and 5/hour per email, answers 60/hour per token); the client address comes only from Caddy's `X-Forwarded-For` through uvicorn `--proxy-headers`; no address is stored. Caddy routes only `weave-studio.in/api/v0/waitlist/*` to the API. The operator script `infra/gcp/singlevm/waitlist.py` lists, exports (CSV), reports, sets status, notes and erases entries; a worker tick deletes submissions untouched for `WEAVES_WAITLIST_RETENTION_DAYS` (365). Required real-stack control TW-01 joins, answers, changes, clears, resumes and re-joins through the API, then verifies the rows with the operator script.

### Phase 4 progress: organization lifecycle, export and deletion

**States.** `ACTIVE`, `SUSPENDED`, `DELETION_REQUESTED`, `DELETING`, `DELETED`. The stored `Organization` payload is unchanged so the previous release's strict reader still works; the richer state, reason, grace deadline, history (`state`, `reason`, `actor_kind` owner/operator/system, `actor_ref`, `at`) and purge checkpoint live in a separate `organization_lifecycle` record (written in the same unit of work as `Organization.status`, under the per-organization administration lock). Every non-active state projects to `status` `suspended` (`deleted` for DELETED), so every phase-1 guard (work admission, workers, schedules, webhooks, OAuth continuation, skills publication, account selection, `resolve_principal_scope`) stops new work with its existing check; `effective_organization_state()` is the single reader that combines both records, and the result is exposed additively as `lifecycle_state` and `deletion_scheduled_for` on `/account/me` and `/account/browser/me` memberships. Minimum rollback image: the previous release reads every stored organization and treats the new states as suspended (fail closed); it ignores the new collection. Resume replays nothing: schedules skip occurrences while inactive (regression test added) and cancelled jobs stay cancelled.

**Who may do what.** The current owner (account session, explicit ownership record) can request deletion (`POST /api/v0/organization/deletion-request`, exact organization name, optional reason; grace `WEAVES_ORGANIZATION_DELETION_GRACE_DAYS`, default 7), cancel an owner-requested deletion within grace (`/deletion-cancel`), read `GET /organization/lifecycle` and export (`POST /organization/export`). While deletion is pending the middleware admits the owner (only) to exactly those three routes through an explicit allow-list; everything else, and every other person, gets the identical `403 Organization not available`. Customer Admins can read the lifecycle view only while the organization is active. Platform operator actions (suspend, resume, request/cancel deletion, `purge-now` with the exact name, status, list) exist only as `infra/gcp/singlevm/organization_lifecycle.py` run on the VM with database-owner access; there is no HTTP operator surface (a test scans the route list), every action requires `--operator` and `--reason`, writes an `organization.lifecycle.*` audit event attributable to the operator (audit actor `operator:<address>`) and a structured log line. Time-limited support access is out of scope.

**Deletion and crash safety.** The worker (`WEAVES_LIFECYCLE_POLL_SECONDS`, default 30) moves expired `DELETION_REQUESTED` organizations to `DELETING`, then purges under the organization's own row-level-security scope in bounded steps (200 rows per collection per tick; cancel queued/running work, revoke credentials, delete secrets through the store, every collection in dependency order, audit retention, a final sweep of unknown collections) and finalizes to `DELETED` only after two consecutive passes delete nothing, under the administration lock (which also serializes a concurrent first sign-in; a non-active organization never gains memberships). Each step is idempotent and commits alone, so a crash converges; failure-injection tests raise at every step. The only new system purpose is `lifecycle` (read `organization_lifecycle`, `organization_settings`, `organizations`) for discovery of pending deletions and the account views.

**Retained, and why.** The `Organization` tombstone (id, status `deleted`, name replaced by `deleted organization`, which the previous reader tolerates) so the id can never be re-created or re-activated; the lifecycle record and history (accountability for who deleted what and when); `organization.lifecycle.*` and `organization.exported` audit events; and `account_organization_requests` (the per-account creation idempotency and quota ledger: a deleted organization still counts toward the cap). Everything else, including other audit events, is removed; the global Account and its other memberships are untouched.

**Export.** Owner-only, bounded (`WEAVES_ORGANIZATION_EXPORT_MAX_BYTES`, default 50 MB, 413 above it), `attachment` with `no-store`, audit `organization.exported`. It excludes secrets, credentials and every digest of a secret, `auth_ref`/`secret_ref`, sessions/cookies/OAuth states, account ids and other tenants; other members' private tasks and personal skills are withheld and counted, while the owner's own personal content is included.

**Not covered.** Deleted data in earlier backups (phase 6), billing/usage records, time-limited operator support access, an in-flight worker's last writes after purge (fenced by lease ownership, tenant-activity checks and the repeated empty-pass rule; `status` reports remaining rows) and public self-service. Required real-stack control `T4-I02`.

## Phase 4 — Deliver safe onboarding and tenant lifecycle

### Changes

- Require verified account authentication before organization creation. Provision org, owner membership/principal, workspace, roles, defaults, and audit atomically and idempotently. Return a human session/selection result, not a permanent owner API key.
- Keep member provisioning compatible with verified Google identity and account memberships; no email-code experience remains. A pending access grant may be claimed only through verified identity under the documented policy. Membership removal is different from disabling a global account.
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
