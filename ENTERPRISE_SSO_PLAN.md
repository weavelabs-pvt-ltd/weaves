# Enterprise sign-in plan

Status: implementation plan, 2026-11-17. The connection and external-identity persistence foundation and an opt-in browser session/CSRF foundation have been added. The live login flow still uses the legacy routes; the frontend has not switched to app-origin BFF traffic. Production uses the single VM; managed deployment scripts are historical. See [SSO configuration and pending work](SSO_CONFIGURATION.md) for setup and rollout requirements.

## Goal and scope

Support organization-specific enterprise sign-in and first-party Google, Microsoft Entra ID, and Zoho sign-in. Keep authentication (proof of identity), provisioning (who may have an account), and authorization (what that account may do) separate. Default to admin-provisioned users and explicit role bindings; add controlled just-in-time (JIT) and SCIM provisioning later. No provider may assign Weaves administrator permissions merely by returning an email or group name.

The first customer-ready milestone is **tenant-scoped OIDC** with Google, Microsoft, and Zoho, safe account linking, SSO enforcement, audited administration, and working deprovisioning. The production browser architecture is a backend-for-frontend (BFF) with a server-side session and a Secure, HttpOnly cookie. SAML 2.0 and SCIM 2.0 are subsequent capabilities driven by customer requirements; neither substitutes for the identity and tenant controls in the first milestone.

## Current code and gaps

| Area | Existing implementation | Required change |
| --- | --- | --- |
| OIDC | `modules/auth/http/sso_routes.py` registers process-wide Google, Microsoft, and Zoho clients from environment variables. Authlib handles code flow, state, nonce, and S256 PKCE. | Resolve one enabled connection by organization before redirect; bind that connection through callback and verify its issuer. |
| Account lookup | Google/Zoho use `email_verified` and email; Microsoft uses a deployment-wide `tid:oid -> email` environment map. `_complete_signin` looks for exactly one matching email across all organizations. | Persist an external identity keyed by organization, connection, issuer, and subject. Never silently link or relink by email. Remove the Microsoft environment mapping after migration. |
| Credentials | `apps/api/app.py` uses a short-lived signed browser session for OAuth transaction state. SSO returns a Weaves bearer token to a popup opener via `postMessage`. | Preserve state/nonce/PKCE; bind state to organization, connection, return origin, and expiry. Move production browser sign-in and API calls behind a same-origin BFF using server-side sessions and Secure, HttpOnly cookies. Stop delivering bearer credentials to JavaScript. |
| Sessions | `modules/auth/runtime_sessions.py` creates revocable 12-hour tokens and checks current user/principal status. | Record authentication method, connection, auth time, and organization policy version; enforce configurable absolute/idle lifetime and revoke on deprovision, connection disable, and enforcement change. |
| Password/email code | `modules/auth/http/session_routes.py` leaves both routes public. | Resolve organization before authentication and enforce its allowed methods server-side, including email-code request/verify and password set/reset paths. |
| Secrets | `infrastructure/secrets/store.py` already has Fernet-encrypted PostgreSQL storage with an external key. | Store only secret references in connection records; use KMS/managed key storage in production, support rotation, and never return secrets through admin APIs. |
| Persistence | `infrastructure/database/store.py` uses a versioned PostgreSQL record store and unique indexes. | Add organization-scoped contracts, repository bindings, database uniqueness constraints, and migrations. |
| Tests | `tests/contracts/test_sso_login.py` exercises Google success, rejected claims, replay, and missing config. | Add tenant isolation, all three providers, linking, enforcement, lifecycle, recovery, and deployment tests. |

## Identity and tenant model

Add these organization-scoped records (names indicative):

| Record | Important fields and constraints |
| --- | --- |
| `SignInConnection` | `id`, `org_id`, `protocol` (`oidc` initially), provider, exact issuer/discovery URL, client ID, `client_secret_ref`, tenant ID / Zoho data center when applicable, status (`draft`, `tested`, `enabled`, `disabled`), timestamps, version. Unique active connection identifier within an organization. |
| `ExternalIdentity` | `org_id`, `connection_id`, issuer, subject, `user_id`, observed email, created/last-seen times. Unique `(connection_id, issuer, subject)` and an explicit rule for whether one user may have multiple identities. Subject and issuer are immutable after creation. |
| `OrganizationSignInPolicy` | `org_id`, allowed methods, `sso_required`, default connection, session limits, optional verified domains, policy version. Every organization starts with its existing methods until migration is complete. |
| `VerifiedDomain` | Canonical domain, organization, DNS TXT challenge digest, verification/expiry times. Globally unique verified ownership; no access is granted merely by matching an email suffix. |
| `SsoTransaction` | One-time random state digest, org/connection, nonce and PKCE verifier or encrypted references, exact callback/return origin, browser binding, expiry and consumed time. Central storage so callbacks work across API replicas. |

Use the existing `User`, `Principal`, `RoleBinding`, and `UserSession` records. Add database-level uniqueness and referential checks, not only Python-level checks. During migration, require an organization identifier (slug or opaque ID) for ambiguous email addresses. Never choose an organization from an unverified email domain. Return uniform public errors where account existence could leak.

## Browser session architecture: decided

Use the BFF pattern described in [RFC 10017, section 6.1](https://www.rfc-editor.org/rfc/rfc10017.html#section-6.1): the server performs the OIDC code exchange, keeps provider credentials and Weaves session state server-side, and serves browser API traffic through the same origin as the app. The browser receives only an opaque, high-entropy session identifier in a `Secure; HttpOnly; SameSite=Strict; Path=/` host-only cookie (use a `__Host-` prefix where supported). Store only its digest server-side; rotate it after sign-in and privilege changes; revoke it on logout, deprovision, and expiry. The existing bearer-token API may remain for non-browser clients, but browser code must never receive a bearer token from login, callback, or `postMessage`.

The current production app is `https://app.weave-studio.in` and the API is `https://api.weave-studio.in` (`infra/gcp/singlevm/Caddyfile`). Route browser `/api/v0/*` and auth callbacks through `app.weave-studio.in` using a dedicated BFF or a trusted reverse proxy to the existing API. Set the frontend's API base to `/api/v0` after the route exists. Keep `api.weave-studio.in` for authenticated machine clients if needed, but never accept the browser session cookie there. Do not set a parent-domain cookie. Validate the single-VM gateway routing before switching the frontend; retired Cloud Run scripts are not deployment targets.

Cookie authentication requires CSRF protection on every state-changing browser route: validate `Origin` against the exact app origin and require a session-bound CSRF token in a custom header, with `Sec-Fetch-Site` as an additional defense. `SameSite` alone is insufficient when another subdomain shares the same site. Avoid permissive credentialed CORS, and reject cross-origin browser requests to the BFF. The OIDC callback remains protected by its one-use state, nonce, and PKCE checks. Add clickjacking and content security headers and avoid untrusted script injection. [RFC 10017, sections 6.1.3.2-6.1.3.3](https://www.rfc-editor.org/rfc/rfc10017.html#section-6.1.3).

The OAuth transaction cookie currently created by `SessionMiddleware` is distinct from the new authenticated browser cookie. Its current `SameSite=Lax` setting supports top-level OIDC redirects; keep transaction state short-lived and use server-side storage for cross-replica handling. Avoid using the browser session cookie as OAuth state. Browser `/auth/me` should return identity and a CSRF token only after the cookie is validated. Logout revokes the server record and clears the cookie; neither endpoint returns a bearer credential.

For all providers, the durable account key is the validated **issuer + subject** within the configured connection. Microsoft additionally checks exact tenant ID and records `tid:oid` for diagnostics/cross-app correlation; `email`, `preferred_username`, and UPN are display/contact data, not authorization keys. Google Workspace restrictions use a validated `hd` claim when configured, and Google `sub` remains the account key. Zoho uses its validated OIDC issuer and `sub`, with the configured regional issuer pinned. Initial binding of an existing Weaves user requires an authenticated admin action or a separately authenticated current user; matching email alone cannot link accounts. [OIDC Core](https://openid.net/specs/openid-connect-core-1_0.html), [Google OIDC](https://developers.google.com/identity/openid-connect/reference), [Microsoft ID token claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference), [Zoho OIDC](https://www.zoho.com/developer/oauth/sign-in-using-zoho-oidc.html).

## Authentication flow and controls

1. User selects an organization or uses a verified discovery path. `GET /api/v0/auth/organizations/{slug}/sso/providers` reveals only enabled sign-in choices for that organization.
2. `GET /api/v0/auth/organizations/{slug}/sso/{connection_id}/start` validates connection status and policy, rate-limits starts, creates one short-lived server-side transaction, and redirects to its pinned authorization endpoint using code flow, S256 PKCE, state, and nonce. No user-controlled issuer, metadata URL, redirect URL, or return origin is accepted.
3. Callback looks up and consumes the transaction exactly once, checks browser binding and the selected connection, exchanges the code, and validates signature, allowed algorithm, issuer, audience, expiry, nonce, and provider-specific tenant/domain claims. Use distinct callback paths or verified issuer binding to prevent IdP mix-up; reject missing or changed claims. TLS and exact registered redirect URIs are mandatory in production. [OAuth Security BCP](https://www.rfc-editor.org/rfc/rfc9700.html).
4. Resolve the external identity inside that organization. If absent, deny by default and show an admin provisioning/linking path. Optional JIT is enabled per organization only after domain ownership and role policy are set; create the user with the least privileged approved role in one transaction. Never accept administrator/group role changes from unconfigured claims.
5. Check organization, user, principal, connection, and role status, then create an authenticated server-side browser session and set its cookie. Preserve the existing bearer-session path for approved non-browser clients. Audit success/failure with reason category, org, connection, and request ID; do not log authorization codes, tokens, secrets, raw assertions, or full claims.
6. On disable/deprovision, atomically disable the user/principal, revoke sessions and relevant API credentials, and remove role bindings according to policy. Existing request authentication must keep checking active status. Back-channel logout/continuous access evaluation can be added for customers that require faster IdP-triggered revocation.

Keep a tested break-glass path: two named organization administrators using strong MFA, monitored use, short sessions, and a recovery procedure. SSO enforcement excludes only those explicitly marked accounts. Never use a shared static API token as a human recovery path. Global developer/bootstrap bypasses in `apps/api/authentication.py` must be off in production.

## Administration and user experience

Add organization-admin APIs and UI to create a draft connection, enter client credentials once, display exact redirect URI, test against the real IdP with an admin account, enable/disable, rotate credentials, inspect audit events, verify domain ownership, and set sign-in policy. Changing issuer, tenant, domain, enforcement, or default connection requires step-up authentication and a second administrator approval for high-risk tenants. Require at least one tested admin route before enabling `sso_required`; provide a rollback path that is audited and time limited.

The login page should offer Google, Microsoft, and Zoho where configured, plus organization discovery. A provider being absent means unconfigured for that organization; the UI must not imply all three are universally available. Complete sign-in with a full-page redirect or a popup that posts only a success/failure signal, then reload `/auth/me`; no credential is posted to the opener. The API uses generic user-facing errors and internal correlation IDs; diagnostic reasons go only to audit/operations logs.

## Phased delivery and acceptance gates

| Phase | Deliverable | Gate to advance |
| --- | --- | --- |
| 0: security baseline | Threat model, production authentication settings, app-origin BFF routing on the single VM, callback/origin inventory, provider test tenants, secret-key management, migration/rollback design. | Security review of identity linking, tenant boundaries, break-glass, cookie security, and CSRF controls. |
| 1: tenant OIDC | Contracts/migrations/repositories; organization-scoped discovery and connection admin APIs; Google, Microsoft, Zoho adapters; immutable external identities; server-side transactions; authenticated browser sessions; audited linking and enforcement; UI. | End-to-end tests with each provider, including duplicate email in two orgs, wrong tenant/issuer, email change, replay, disabled user, enforcement bypass, CSRF, cookie flags, and cross-replica callback. |
| 2: lifecycle | SCIM 2.0 Users/Groups, per-org scoped hashed bearer credentials, idempotent PATCH, role mapping with allowlisted groups, immediate deprovision/revocation, JIT policy where needed. | Provider interoperability tests and measured deprovision latency; no role escalation from unmapped groups. [SCIM protocol](https://www.rfc-editor.org/rfc/rfc7644.html). |
| 3: SAML | Per-org SAML connection, metadata/certificate rotation, signed AuthnRequest/response policy, assertion signature and audience/recipient/time/replay checks, SP metadata, attribute mapping. | Independent security review and interoperability tests with two enterprise IdPs. |
| 4: operations | Admin self-service refinements, alerting, secret rotation, session policy, incident runbooks, staged customer migration. | Pilot customer sign-off, rollback drill, audit export, support runbook, and production monitoring. |

Phase 1 is the minimum release for an enterprise customer using OIDC. Customers requiring automated provisioning or SAML are ready only after the respective phases. Roll out with feature flags by organization: import existing users, invite/approve identity links, test all admins, monitor sign-ins, then enforce SSO. Keep old environment-based SSO only as a temporary migration route and remove it after all connections are moved; do not let it bypass a customer's organization policy.

Before deploying migration 19, verify a rollback target that accepts the new ledger version. The previous application image rejects versions above 18, so the existing automatic image rollback cannot recover it after the migration. Prepare and test a compatible baseline release or a separately reviewed recovery strategy first; this PR must not deploy until that gate passes.

## Implementation order in this repository

1. Add contracts under `contracts/v1`, repositories in `application/runtime_repositories.py` and `infrastructure/persistence/product_repositories.py`, migration/indexes in `infrastructure/database/store.py`.
2. Add an identity/connection service under `modules/auth`, compose it in `apps/runtime_services_auth.py`, and expose only admin-authorized configuration routes.
3. Add app-origin BFF routing in `infra/gcp/singlevm/Caddyfile`, then add browser session authentication and CSRF enforcement in `apps/api/authentication.py` without weakening bearer authentication for machine clients.
4. Replace the global client selection in `modules/auth/http/sso_routes.py` with organization-scoped connection lookup and one-time transaction storage. Preserve provider-specific validation in small adapters; set the browser cookie on success.
5. Enforce organization sign-in policy in `modules/auth/runtime_sessions.py`, `modules/auth/runtime_email_login.py`, and the HTTP route layer. Attach method/connection metadata to sessions and audit events.
6. Extend `tests/contracts/test_sso_login.py` and integration tests for all acceptance cases above; verify browser callback behavior and production deployment configuration.

## Remaining product inputs

The browser architecture is decided above. Use a Weaves-managed OAuth client for initial Google sign-in where provider policy permits; support customer-owned clients for enterprise connections, especially Microsoft and Zoho. Before configuring a specific customer, obtain its tenant ID, Zoho data center, domain ownership, IdP metadata, and any SCIM/SAML requirement. These are onboarding inputs rather than blockers to building the core architecture.
