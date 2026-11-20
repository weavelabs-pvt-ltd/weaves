# Multi-tenancy and SaaS readiness audit

Date: 2026-11-20. Assessment only; application code and production were not changed.

Backend: `d70176dfbe3c328e6e3070fa2f79f5c123a23db2`.
Frontend: `6f6ed9888b398921e2d567e0671b954df64185e8` in the sibling `weaves-app` repository.

## Assessment

Weaves already has meaningful multi-tenant foundations: organization-owned identities, workspace membership, scoped repositories, tenant-aware resource validation, scoped idempotency, and background jobs carrying organization/workspace identity. It is not yet ready for unrelated customers to use as a self-service SaaS.

The immediate problems are specific boundary failures, rather than an absence of tenant IDs everywhere. Five findings below have direct behavioral reproductions. The remaining findings distinguish missing product capabilities from infrastructure hardening. A shared PostgreSQL database and shared application deployment are valid SaaS architecture; separate databases or Kubernetes are not prerequisites.

Priority meanings:

- **P1:** fix before onboarding unrelated customers, or before enabling the affected feature.
- **P2:** required SaaS foundations and scale/operational work; timing depends on launch scope.
- **P3:** broader tenant configuration and enterprise capabilities when offered.

## Scope and validation

Reviewed the API route composition and tenant propagation through authentication, organizations/workspaces, RBAC, agents/versions, tasks/transcripts/private skills, workflow definitions/studio/triggers/schedules, execution workers/recovery, evaluations, approvals/audit, connectors/OAuth, models/secrets, persistence, frontend session/workspace state, and checked-in single-VM deployment/backup configuration. Source inventory: 277 backend Python files, 52 test modules, and 17 top-level frontend JavaScript files. Inventory counts describe the codebase, not a claim that every line received equal inspection.

Validation performed:

- Ran the existing backend suite against the checked-out source, including PostgreSQL integration tests in disposable schemas on the local development PostgreSQL instance: **317 passed**, four dependency/runtime warnings, 38.82 seconds.
- Created two fresh organizations with the same owner email in an isolated in-memory API runtime. Used real application routes and services, with email captured and outbound HTTP replaced by `httpx.MockTransport`.
- Reproduced custom connector metadata leakage, ambiguous email login, unsafe provider destination dispatch, archived-workspace task execution, and archived/suspended webhook execution.
- Verified denial controls: foreign organization agent/workflow/task/private-skill-version IDs returned **404**, a foreign workspace header returned **403**, and resource lists excluded the other organization's agents and private skills.
- Executed the actual frontend session-expiry and logout function bodies in a Node VM with stubbed browser dependencies to inspect retained state and draft keys. This proves state retention; it is not a complete two-user browser automation test.

No production database was queried or modified for these reproductions. No real internal-network request, provider charge, or outbound email was made. Checked-in infrastructure was reviewed; current live configuration was not re-audited.

## Confirmed behavioral defects

### 1. P1 — Custom MCP connector metadata leaks across organizations

**Evidence:** [descriptor listing](modules/integrations/admin_service.py#L88), [HTTP exposure](modules/agents/http/routes.py#L128), [custom descriptor creation](modules/integrations/runtime_mcp_installations.py#L105).

`list_plugin_descriptors()` returns `self.plugins.list()` for an authorized workspace user. Built-in descriptors are legitimately global, but custom MCP installation creates a descriptor in the same repository. Its display name, description, capability names/descriptions, and schemas can contain customer-specific information; the descriptor has no tenant ownership attached.

**Reproduction:** organization A installed a custom MCP connector named “Tenant A PRIVATE payroll.” Organization B requested `GET /api/v0/plugin-descriptors`, received **200**, and received that descriptor and its “Tenant A confidential payroll schema” description. B's installation list remained correctly empty. This is an actual cross-organization metadata disclosure, not credential disclosure or proof that B can invoke A's tools.

**Fix:** distinguish global built-in catalog definitions from organization/workspace-owned imported descriptors. Scope custom descriptor creation, listing, lookup, cleanup, and API serialization; preserve global visibility only for genuinely public definitions. Add a two-tenant API regression test that installs through the real route.

### 2. P1 — A tenant admin can make model-provider requests to internal destinations

**Evidence:** [provider creation](modules/models/runtime.py#L262), [URL validation](contracts/v1/models.py#L60), [outbound request](clients/model_providers/openai_compatible.py#L32).

The API accepts an organization admin's `base_url`, validates only the HTTP(S) shape and credential placement, and immediately contacts it for model discovery. It does not reject loopback/private/link-local destinations or require approved provider origins. This exposes the shared server's network authority to tenant administrators. Depending on reachable services, the consequence can extend beyond that tenant.

**Reproduction:** provider creation with `base_url=http://127.0.0.1:8001/api/v0` returned **201** with a mocked successful catalog. The transport recorded an attempted request to the loopback destination. No real connection was made. This establishes missing destination enforcement, not exploitation of a live internal service.

**Fix:** fix canonical destinations for hosted providers; explicitly govern custom endpoints. Enforce HTTPS, permitted ports, address classification including IPv6 and DNS resolution/rebinding, and equivalent policy on redirects. Add network-level egress restrictions. The MCP client already has a host allowlist and disables redirects; preserve those protections and review allowlisted hosts' resolved addresses too.

### 3. P1 — Workspace archive / organization suspension does not stop background execution

**Evidence:** [API tenant-status validation](modules/auth/runtime_organizations.py#L304), [permission checks](modules/auth/runtime_credentials.py#L299), [task worker](modules/tasks/worker_service.py#L52), [execution worker](modules/executions/runtime_worker.py#L22), [webhook admission](modules/workflows/runtime_triggers.py#L296), [studio scheduler](modules/workflows/studio_scheduling.py#L17).

The API middleware resolves an active organization and accessible active workspace. Workers, schedulers, and signed webhooks set context directly from persisted records. Their permission checks validate the principal and roles, but do not perform the same organization/workspace lifecycle checks.

**Reproduction:** after archiving a workspace, ordinary API access returned **403**, but a previously queued task **succeeded**. Its correctly signed webhook still queued a job. After setting the owning organization to `suspended` in the isolated repository, another webhook still queued work; both queued workflow jobs then **succeeded** while the organization was suspended.

**Fix:** a shared tenant-eligibility guard at all admission and execution boundaries, including worker claim/start, schedule dispatch, webhook admission, workflow advancement, and approved external actions. Define whether running work is cancelled or allowed to finish at a safe boundary. Tenant suspension must prevent new work and resource spending consistently; retain explicit system/recovery paths for cleanup.

### 4. P1 — Frontend authentication boundaries do not fully clear customer state

**Evidence:** [session expiry](../weaves-app/dashboard.js#L90), [logout](../weaves-app/dashboard.js#L3508), [refresh](../weaves-app/dashboard.js#L2406), [agent draft persistence](../weaves-app/dashboard.js#L1093), [workflow draft persistence](../weaves-app/workflows.js#L12).

Explicit logout clears authentication and selected identity fields but retains resource arrays and builder state. Session expiry clears more collections, but still retains agent builder instructions, home prompt text, skill library/version cache, and other feature state. Refresh clears the skill caches, which reduces persistence after a successful new load, but does not constitute a complete authentication-boundary reset.

Agent and workflow drafts use keys based only on workspace ID: `weaves.agentDrafts.<workspace>` and `weaves.workflowDrafts.<workspace>`. Two accounts using the same workspace and browser share these draft stores. Different workspace IDs do separate those persisted stores; this is not evidence that the keys alone leak across organizations.

Some asynchronous results are guarded by workspace/page or route request only, rather than an authentication generation. The API wrapper also treats a delayed 401 from an older request as a reason to expire whichever session is current when it arrives. An old request completing after logout/login can repopulate shared state. The Skills mount adapter already includes auth token/org/workspace in its scope, which is a useful pattern to extend.

**Reproduction:** executing the actual session-expiry function retained the confidential builder instructions, home prompt, and private skill cache inserted into its state. Executing the actual logout block retained the agent collection and builder. Draft key inspection confirmed neither key includes a principal ID. Unauthorized server-side access or an automatically rendered cross-user disclosure was not demonstrated by this probe.

**Fix:** one complete teardown used by logout, expiry, user changes, and tenant/workspace changes; destroy controllers, close dialogs, stop polling, cancel requests, and reject responses from old auth/scope generations. Namespace private drafts by org + workspace + principal, with an explicit migration/cleanup policy for existing keys. If drafts are intended to be shared, store them on the server behind explicit permissions instead.

### 5. P1 — The same email in two organizations breaks the normal sign-in journey

**Evidence:** [password matching](modules/auth/runtime_sessions.py#L189), [email-code matching](modules/auth/runtime_email_login.py#L126), [SSO completion](modules/auth/http/sso_routes.py#L49), [frontend code request](../weaves-app/email-login.js#L11).

The data model permits the same email in separate organizations. Password and email-code APIs support an optional `organization_id`, but the normal frontend does not collect or send it. Their matching scans all users and requires exactly one match. SSO also matches the email globally without organization selection.

**Reproduction:** the same active email existed in A and B. An unscoped email-code request returned **202** with an opaque challenge ID, but no challenge was persisted or email sent. Providing A's `organization_id` did persist the challenge. The generic response is intentionally anti-enumeration; the missing tenant-resolution journey is the defect.

Account rate-limit buckets are also keyed by email without organization, so activity against one tenant account affects another tenant account with that email. Global IP/abuse limits remain useful; tenant-account throttling should be a separate layer.

**Fix:** implement a deliberate tenant-discovery/sign-in journey. Use verified identity followed by authorized organization choice, or an explicit organization identifier before tenant-scoped authentication. Retain non-enumerating responses. Resolve SSO using the selected organization's trusted issuer/subject binding. Add tests for the same email in two organizations across every login method.

## SaaS architecture and product gaps

### 6. P2 — No global account membership / organization-switching model

**Evidence:** [User and Principal](contracts/v1/identity.py#L92), [organization endpoint](modules/auth/http/workspace_routes.py#L31), [frontend identity loading](../weaves-app/dashboard.js#L2406).

A user and principal each belong to exactly one organization. `/organizations` returns only the current organization, and the UI switches workspaces inside that organization. There is no global human account with memberships in multiple customer organizations, organization chooser, or switch operation.

This model can securely support separate tenant-specific accounts; it is not itself an isolation vulnerability. If one sign-in must access multiple organizations, add a global account and organization-membership layer, retaining tenant principals, per-org roles/status, and existing resource ownership. Derive the selected tenant from authenticated membership; do not simply trust a new `X-Org-ID` header.

### 7. P1 when public signup is enabled — Onboarding is provisioning, not verified signup

**Evidence:** [public onboarding route](modules/auth/http/user_routes.py#L123), [active owner creation](modules/auth/runtime_organizations.py#L147), [owner credential](modules/auth/runtime_organizations.py#L202), [production compose disables onboarding](infra/gcp/singlevm/compose.yaml#L37).

Organization creation accepts a claimed owner email, creates an active owner immediately, and returns a full-owner API bearer credential with `expires_at=None`. There is no mailbox-proof step before ownership is granted. The endpoint can be disabled or protected by a deployment-wide bootstrap token, but neither is a customer signup flow.

**Reproduction:** A and B were created with the same claimed owner email without email proof; both owners were active and the initial API credential had no expiry. Combined with finding 5, uncontrolled signup could also create ambiguous email matches for an existing customer's login.

The checked-in production configuration disables onboarding, so this is a blocker to enabling self-service registration, not a claim that production currently exposes the route.

**Fix:** verify the human identity first; atomically provision the organization/membership/defaults; issue a short-lived human session rather than a permanent owner API credential. Add idempotent onboarding, abuse controls, terms/consent as required, and explicit failure/retry states. Create service/API credentials separately.

### 8. P2/P3 — Organization-specific enterprise authentication is still a foundation

**Evidence:** [active provider configuration and sign-in](modules/auth/http/sso_routes.py#L20), [subject-binding service](modules/auth/sso_identity_service.py#L26), [browser session endpoints](modules/auth/http/browser_routes.py#L16), [session contract](contracts/v1/auth.py#L30), `ENTERPRISE_SSO_PLAN.md`.

The active SSO flow uses deployment-level provider configuration. Microsoft selects one configured Entra tenant; successful sign-in resolves a globally matched verified email rather than the new organization-specific external-identity bindings. The new `SsoConnection` / external-identity / browser-session foundations exist, but are not the complete customer-facing enterprise SSO flow.

There is no complete organization security policy enforcing allowed login methods, SSO-only access, tenant-specific session duration, or identity-policy version. Password/email-code login does not implement an org-specific SSO enforcement rule. The frontend still stores bearer sessions in `sessionStorage`; cookie-only browser endpoints exist but are not adopted end to end.

**Fix when offering enterprise SSO:** finish tenant-bound start/callback transactions, issuer/subject binding, connection administration, trusted discovery/domain policy, cookie issuance/frontend adoption, method enforcement and recovery controls. A shared Google OAuth application is normal SaaS architecture; it need not be duplicated per customer. Mandatory SSO, MFA, SCIM, and verified domains are launch-scope decisions, not prerequisites for all basic multi-tenant products.

### 9. P2 — Tenant isolation depends on caller discipline; persistence does not enforce it

**Evidence:** [record schema](infrastructure/database/store.py#L17), [unrestricted upsert](infrastructure/persistence/postgres_repository.py#L70), [scoped read](infrastructure/persistence/postgres_repository.py#L111), [repository interface](application/ports/repository.py#L20), [default context](shared/request_context.py#L30).

Scoped queries work, but the same repository exposes unrestricted `get/list/delete/put`. Upsert conflicts on `(collection, record_id)` and can replace ownership columns. The database has no tenant RLS policies or relational ownership constraints linking JSONB references. Tenant fields are nullable because the table also contains global records. Request-context helpers silently default to `local-org` / `local-workspace` when no identity is bound.

These are architectural bypass opportunities for future mistakes, not proof that current HTTP routes allow arbitrary record replacement. With production local seeding disabled, a missing context often fails because local records do not exist; it should fail explicitly instead.

**Fix:** separate tenant-scoped application repositories from explicitly privileged system/catalog access; make production tenant context mandatory; use updates/deletes with ownership predicates and immutable tenant ownership. Validate composite references. Add suitable schema constraints and evaluate RLS as defense in depth, with explicit global/system paths and a non-bypass application DB role. Do not enable RLS without accounting for global catalogs, authentication bootstrap, and cross-tenant worker dispatch.

### 10. P2 — Secret storage has encryption, but no tenant ownership boundary

**Evidence:** [secret table and adapter](infrastructure/secrets/store.py#L68), [secret port](application/ports/secrets.py).

Provider/connector credentials are encrypted, and normal resource lookups are scoped before resolving them. However, the secret table and API identify a secret only by opaque `secret_ref`, without org/workspace ownership or purpose. One deployment encryption key protects all tenants.

An unguessable reference and scoped callers reduce exposure; no cross-tenant credential retrieval was demonstrated. The missing ownership model matters for authorization checks, tenant deletion/export/rotation, support tooling, and blast radius.

**Fix:** attach immutable owner/purpose metadata and require authorized tenant context for put/get/replace/delete. Keep deployment secrets explicitly separate. Implement key IDs and rotation; envelope encryption/per-tenant data keys can be added if the isolation/recovery requirements justify them. A separate KMS key per tenant is not automatically necessary.

### 11. P1 minimum controls / P2 full scheduler — No tenant resource admission or fairness

**Evidence:** [single worker loop](apps/worker/main.py#L41), [global job claim](modules/executions/runtime_worker.py#L22), [global turn claim](modules/tasks/worker_service.py#L52), [per-run budgets](contracts/v1/agents.py#L43), [shared pools](infra/gcp/singlevm/compose.yaml#L2).

Agent budgets bound individual runtime/tool/token/cost behavior. They do not cap organization-level concurrent executions, queued work, requests, stored records, seats, schedules, or monthly usage. The worker processes shared queues synchronously; API/DB/HTTP pools and VM resources are shared. A tenant's long run or large backlog can delay others. Idempotency lock maps also grow with distinct keys without an evident eviction policy.

**Fix:** tenant-aware admission limits and queue-depth/concurrency caps first; fair scheduling and explicit workload classes next. Separate long-running work from request handling and bound lock/cache lifetimes. Add atomic usage reservation/settlement if Weaves pays for model calls. Keep global emergency caps as well as tenant limits. A shared worker fleet can satisfy this; one queue/database per tenant is not mandatory.

### 12. P2 if commercial SaaS — Billing, plans, and entitlement enforcement are absent

**Evidence:** [organization contract](contracts/v1/identity.py#L78), [organization provisioning](modules/auth/runtime_organizations.py#L116), [model/run usage contracts](contracts/v1/model_execution.py), [run cost handling](modules/agents/runtime_run_targets.py).

There is no subscription/customer/invoice/plan/entitlement domain or billing webhook flow. Usage and cost are recorded for execution, but there is no billing-grade tenant ledger, aggregation/reconciliation, billing period, or atomically enforced plan limits.

**Fix:** decide free/BYOK/managed usage policy, introduce plan-independent tenant entitlements, and enforce them at authoritative write/admission boundaries. Build billing events idempotently with immutable usage attribution and provider-event reconciliation. Paid billing can wait for a free private launch; resource-abuse controls cannot.

### 13. P2 — Organization lifecycle, deletion, and control-plane operations are incomplete

**Evidence:** [organization statuses](contracts/v1/identity.py#L15), [organization route surface](modules/auth/http/workspace_routes.py#L21), [user suspension/revocation](modules/auth/runtime_users.py#L265), [workspace lifecycle](modules/auth/runtime_workspaces.py#L134).

User suspension, credential/session revocation, and workspace archiving exist. Organization `active/suspended/deleted` statuses exist, but no complete tenant provisioning/suspend/resume/delete/export workflow or operator control plane is exposed. There is no coordinated removal/anonymization covering records, private skill snapshots, external identities, OAuth state, secrets, jobs, logs, and retained backups.

**Fix:** define an explicit organization state machine, billing/security suspension reasons, owner transfer and recovery, and an idempotent deletion workflow with retention/legal-hold semantics. Separate platform-operator authority from customer organization Admin roles; any support access should be narrow, time-bounded, and audited. This is a missing operational capability, not a recommendation to grant org admins global access.

### 14. P2 — Unbounded reads and global scans will couple tenant performance

**Evidence:** [email lookup](modules/auth/runtime_email_login.py#L126), [global collection reads](infrastructure/persistence/postgres_repository.py#L153), [scoped collection reads](infrastructure/persistence/postgres_repository.py#L182), [recovery scans](modules/executions/recovery_service.py#L75), [studio polling](modules/workflows/studio_scheduling.py#L17), [frontend refresh](../weaves-app/dashboard.js#L2406).

Login materializes all users to find an email, including when an org is supplied. Many resource lists materialize an entire tenant collection without pagination. Recovery scans entire run/job collections, studio ticks scan settings/runs, and frontend refresh/list views fan out multiple requests. Home explicitly follows every task cursor to exhaustion to calculate its overview, undoing the bounded backend pagination for that screen. Tasks and audit already have bounded/keyset query paths, which should be preserved and extended.

**Fix:** indexed tenant-email/subject lookups; filtered status/due queries; bounded pagination and aggregate overview endpoints. Bound catalog/list response sizes and batch version/run summaries. Global system discovery can be legitimate; it must be bounded and deliberate rather than loading every customer's full history on every poll.

### 15. P2 — Tenant observability and incident controls are insufficient

**Evidence:** [worker log fields](apps/worker/main.py#L17), [API composition](apps/api/app.py), [shared HTTP pool](infrastructure/http/pool.py), [single-VM services](infra/gcp/singlevm/compose.yaml).

Audit events carry tenant identity and can be searched/exported. Operational logs primarily identify workers/jobs/turns, without a consistent org/workspace/principal/request correlation envelope. No tenant-aware latency/error/queue-age/usage dashboards, customer kill switch, or alerting rules were found in the checked-in application/infra.

**Fix:** propagate request/tenant identity through execution and external calls, emit structured logs with payload/secret redaction, and collect per-tenant usage and appropriately aggregated operational metrics. Avoid unbounded tenant IDs as high-cardinality metric labels; use logs/traces or controlled dimensions. Add a supported tenant incident workflow. Do not confuse product audit history with service monitoring.

### 16. P2 — Backups and recovery are deployment-wide

**Evidence:** [whole-database backup](infra/gcp/singlevm/backup.sh#L9), [restore check](infra/gcp/singlevm/check_backup.sh), [single-VM deployment](infra/gcp/singlevm/compose.yaml).

Backup and restore checks exist. The logical backup is a full shared-database dump; there is no tenant-selective export/restore procedure or tenant retention/deletion reconciliation. Application, worker, and database share one VM, so host failure and resource exhaustion affect everyone. This is a consciously inexpensive personal-test deployment, not proof that logical multi-tenancy requires different hosting.

**Fix:** define recovery objectives and validate a tenant export/restore procedure with dependency/ownership checks. Document backup retention and deletion behavior. For external customer availability requirements, add appropriate off-host recovery/HA and capacity controls. Managed database or a larger/more redundant deployment should follow measured capacity and promised service levels.

### 17. P3 — Tenant settings are too narrow for a broader customer base

**Evidence:** [organization/workspace contracts](contracts/v1/identity.py#L78), [calendar implementation](modules/workflows/studio_context.py#L71), [workflow UI](../weaves-app/workflows.js).

Organization settings are essentially name/status; workspace settings add environment. Calendar scheduling hardcodes `Asia/Kolkata` and the UI labels it IST. There is no organization-level timezone/locale/security-policy/settings model. Custom domains and branding are also absent, though neither is necessary for a first multi-tenant launch.

**Fix:** explicit tenant defaults and per-workflow timezone where needed, UTC storage with clear display semantics, and validated settings administration. Keep business configuration separate from deployment configuration. Prioritize settings that affect execution or security before cosmetic branding.

### 18. P1/P2 — CI needs an adversarial tenancy regression matrix

The current suite covers selected cross-workspace/tenant denials, RBAC, SSO identity constraints, scoped persistence/idempotency, and cross-process PostgreSQL behavior. All 317 tests passed while the reproduced defects above remained present. Add targeted regression tests alongside each fix, plus a shared two-org/two-workspace/two-user fixture rather than relying on isolated local-org happy paths.

Required matrix:

- Reads, list/search/export, writes, deletes, related version IDs, and deliberately foreign nested references for each resource family.
- Same email in two organizations, all authentication methods, account throttling, membership removal and tenant switching.
- Personal skill/task visibility across users in the same workspace, cross-org versions/publications, and snapshots in execution summaries.
- Custom connector descriptor visibility, credential rotation, OAuth callback/continuation scope, and secret ownership.
- Jobs, signed webhooks, schedules, workflow continuation, and approvals after workspace archive, principal disablement, membership removal, and org suspension.
- Concurrent tenants using the same idempotency key and concurrent quota admission across processes.
- Actual browser logout/login/switch with drafts, private caches, delayed responses, pollers, and open dialogs.
- Provider egress policy, schema ownership immutability, tenant deletion/export/restore, and permitted privileged system access.

Use real PostgreSQL for isolation/atomicity/constraint tests, and browser automation for UI identity transitions. Run these checks before both backend and frontend production deployments.

## Existing protections to retain

| Area | What already works / distinction |
|---|---|
| Request identity | PostgreSQL API mode requires credentials, derives organization from the active principal, checks organization status and authorized workspaces, and resets request context. |
| Agents/workflows/tasks | Core collection and known-ID access is scoped; the new probes rejected foreign agent/workflow/task IDs. Versions and referenced agents/models are validated within their owning scopes. |
| Skills | Personal ownership and org visibility checks exist; publication creates explicit org releases; the probe rejected a foreign private version. Skills are intentionally org-level, rather than necessarily workspace-level. |
| Models | Providers/profiles/default model configuration are organization-scoped; configuration is not simply one global model key for every customer. |
| Integrations | Installations and resource selections are workspace-scoped. OAuth state binds org/workspace/principal, and runtime dispatch checks installation/agent scope. The descriptor catalog is the separate leak identified above. |
| Jobs | Task turns/execution jobs store tenant/principal identity and restore context. Atomic claim/lease fencing and recovery exist. Missing lifecycle eligibility is distinct from losing tenant identity entirely. |
| Idempotency | Durable keys/unique indexes include org/workspace/principal for relevant operations. Global in-process lock maps need bounds, but are not proof of cross-tenant replay. |
| Governance | Approvals/policies are workspace-scoped; audit events carry tenant identity and have bounded search/export. |
| Secrets | Encryption, opaque references, secret redaction, and provider resolution behind scoped resources already exist. Add ownership enforcement to this foundation. |
| Infrastructure | Shared DB/processes and shared public OAuth applications are legitimate architecture choices. Isolation and resource governance must be strengthened around them. |

## Recommended implementation order

1. **Close reproduced boundaries:** descriptor ownership, outbound destination enforcement, shared tenant lifecycle guard, complete frontend teardown, tenant-aware login. Ship regression tests for each.
2. **Settle the account model and onboarding:** global account plus org memberships if cross-org sign-in is required; otherwise explicit tenant-specific account discovery. Keep API keys tenant-bound. Add verified, idempotent signup and organization switching.
3. **Enforce persistence and lifecycle invariants:** mandatory scope for normal services, restricted system repositories, immutable ownership, reference validation, tenant-owned secrets, suspend/delete/export workflows. Evaluate RLS after these access paths are explicit.
4. **Add minimum SaaS controls:** tenant queue/concurrency/storage/request limits, fair worker admission, indexed/bounded queries, usage attribution, operational correlation, incident and recovery procedures. Add billing/entitlements if monetizing.
5. **Complete optional enterprise capabilities:** organization SSO policy and administration, MFA/SCIM where offered, configurable timezone/locale and other customer settings, then hosting redundancy appropriate to promised availability.

A practical target is pooled SaaS: global accounts, explicit organization memberships, organizations owning configuration and data, workspaces partitioning operational resources, and a tenant context enforced consistently at API, worker, database, cache, secret, and frontend boundaries. This can be delivered incrementally on the current architecture.
