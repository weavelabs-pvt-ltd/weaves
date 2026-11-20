# ADR: pooled SaaS identity and ownership boundaries

Status: accepted implementation direction for Phase 0; later phases implement the target. Date: 2026-11-20.

## Identity and access

The organization is the customer security/data boundary. Workspaces partition operational resources inside an organization. Global built-in catalogs are explicitly public definitions; customer-imported schemas and connector metadata are not global catalog content.

Introduce global human accounts and explicit organization memberships in Phase 3. Retain tenant-local user/principal IDs and all existing authorship, role, private-skill, task and audit references. A global account carries no implicit tenant permissions. Service accounts and API credentials remain tenant/workspace-bound. Customer Admin is not platform operator.

Do not automatically merge legacy users by email. Verify account ownership and linking independently of claimed email strings and preserve enterprise issuer/subject trust boundaries. Phase 0's shared-person fixture deliberately uses two separate tenant users with the same email and different roles; it does not fake a membership feature that does not exist yet.

Account-level browser authentication will use the cookie/CSRF foundation. Per-tab org/workspace selection is a request to select scope, validated against current membership on every operation. A mutable shared-cookie current-org field must not redirect another tab's writes. API keys cannot change tenant using request headers.

## Ownership inventory

The machine-readable contract is [tenancy_harness/ownership.json](tenancy_harness/ownership.json): all 45 current persisted collections, auxiliary tables, and 135 registered API operations. It records org/workspace/global ownership, conditional private-content rules, lifecycle requirements, and explicitly privileged system paths. CI compares the inventory with actual repository specs and registered routes; changes require review of their ownership contract.

Current invariants and target gaps remain separate. In particular, imported MCP descriptors currently share the global `plugins` repository; the registry calls out the gap, and diagnostic T0-D01 records it. The registry is a reviewed access contract, not an assertion that Phase 2's database enforcement already exists.

Personal skills/tasks remain private to their owner within their organization. Derived versions, snapshots, transcripts, runs and artifacts inherit the relevant parent audience; indirect access must not bypass private visibility. Organization-wide releases are explicit publications. A role check alone does not authorize private content.

Normal tenant services require a validated org/workspace/principal context. Public authentication endpoints require the appropriate proof/state; signed webhooks derive tenant identity from the validated trigger. Cross-tenant discovery for authentication, dispatch/recovery and maintenance uses narrow system interfaces with bounded results; it is not a general Admin bypass.

## Lifecycle semantics

Suspension/deletion and workspace archive prevent new tenant work, including worker starts, webhooks, schedules, workflow continuation and external approved actions. Revalidate at admission/execution boundaries and propagate cancellation to running work at safe boundaries. Already dispatched external actions may have uncertain outcomes; record uncertainty instead of automatically repeating them. Explicit bounded recovery/cleanup is allowed and audited. Resume does not silently replay missed schedules or cancelled work.

Phase 0 records the current violation in T0-D03; Phase 1 implements the common guard. Account disablement, organization membership removal, org suspension, and workspace archive have different scopes. Removing access to A must not unnecessarily disable access to B.

## Compatibility and migration

Preserve current data, resource IDs, immutable version history, tenant-local identities, encryption keys, and tenant-bound API credentials. Use reader-first compatibility, additive schema, resumable verified backfill, feature activation, and later contract cleanup. JSONB readers reject unknown fields, so additive SQL alone is not rollback proof. Every schema/record-shape change names and tests its minimum compatible API/worker rollback image. Never reset/restore the production database as an automatic image rollback.

Shared PostgreSQL/application infrastructure is retained. Phase 2 introduces scoped storage, secret ownership, and staged RLS with separate application/migration/system privileges. Phase 5 adds measured resource admission/fairness; Phase 6 proves recovery. No new hosting resources are created by Phase 0.

### Connector ownership activation (migration 20)

The compatible API/worker keeps `PluginDescriptor` JSON unchanged. New connectors write their organization into the existing record scope column. Before activation, readers safely resolve legacy references in SQL. Migration 20 adds `product_plugin_ownership` and a rollout record, backfills one proven organization, quarantines ownerless/conflicting/invalid legacy definitions, and protects owner identity and installation references with database guards. Disabled references still count. Persisted quarantine survives removing every connection and cannot silently become another organization's descriptor.

Application startup recognizes migration 20 but never applies it. Deploy the same compatible immutable image to API and worker first, validate both, and retain that image as the minimum rollback target. Then run the operator command from that image with its operator-selected `DATABASE_URL` already supplied through the environment:

```sh
python -m apps.cli.plugin_catalog_ownership
python -m apps.cli.plugin_catalog_ownership --apply --reader-image "$VERIFIED_READER_IMAGE"
```

The first command is read-only and returns IDs/classification/reasons, never tool schemas or credentials. `VERIFIED_READER_IMAGE` must be an immutable image digest (`registry/image@sha256:...`; local rehearsals record the actual image content ID). The tool records the supplied verified image; it does not itself prove which image is deployed. Do not activate while any API/worker process still uses an incompatible image. The integration suite starts API on schema 19, explicitly activates 20, starts the native worker, and verifies scoped tools and browser cleanup on the migrated store.

Activation is one atomic transaction under the shared migration lock. It bounds the entire record store to 10,000 rows, including work needed by the partial index build; larger stores are refused and require a separately rehearsed concurrent-index/batched migration. Failure leaves schema 19 and business payloads intact; a retry after success reads immutable ownership rather than reclassifying from changed references. Startup rejects a recorded activation with missing tables, guards, or rollout evidence. Unknown later migration versions remain rejected.

After activation, an organization can disconnect its own quarantined connections and destroy those credentials; the descriptor remains quarantined for operator investigation. A replacement must be created through the normal scoped connection flow rather than assigning ambiguous historical metadata to an organization. Descriptor deletion and new installation creation serialize on the ownership row, preventing dangling references. Separate operator resolution/purge policy and organization deletion remain lifecycle work. Database reset or rollback to a pre-compatible image is not a recovery mechanism.

## Evidence policy

Passing isolation controls and observed known defects are separate report categories. Phase 0's required job validates a reproducible foundation, not SaaS readiness: `saas_ready` is explicitly false. The five documented diagnostics are not xfails/skips or a security certification. Unexpected outcomes fail the baseline; Phase 1 replaces each diagnostic with an actual passing security regression and removes its open-defect entry.

See [the phased implementation plan](MULTI_TENANCY_IMPLEMENTATION_PLAN.md) for launch gates and [the harness instructions](tenancy_harness/README.md) for exact commands and fixture limitations.
