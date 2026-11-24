# Phase 6 design: recovery and private-beta release readiness

Status: design only, 2026-11-24. Nothing here is implemented. Scope comes from
`MULTI_TENANCY_IMPLEMENTATION_PLAN.md` (Phase 6, migration protocol, testing matrix) and
`docs/STATUS_AND_BACKLOG.md` (section C, E1/E2). Schema head is **28** (`bounded_queries_schema.py`
header "Migration 28"; Phase 5.5 added none), so new migrations start at **29**. Prices are
ASSUMPTIONS to re-check on the cited pages before the owner approves anything. Phase 5.6 (load test)
is another agent's work and is not touched; Phase 6 does not depend on its numbers except 6.9.

## 1. Current state (evidence)

### 1.1 Backups
- On-host dump only: `infra/gcp/singlevm/backup.sh:7-12` runs `pg_dump --no-owner --no-acl | gzip` into
  `/opt/weaves/backups/singlevm/` (root-only 0700/umask 077), `gzip -t`, and deletes files older than 7
  days (`:12`). The dump is **plaintext** gzip SQL: it holds emails, token digests, audit data and
  Fernet-encrypted secret ciphertext. It is on the same disk as the database it protects.
- Schedule is not in the repo. README "Backup and restore" (`README.md:87-90`) says a cron at 19:45 UTC
  calls `/opt/weaves/singlevm-backup.sh` and a snapshot policy runs at 20:00 UTC; the only code evidence
  is `infra/gcp/personal/provision.sh:25-27` (snapshot-schedule `weaves-personal-daily`, daily, 7 days,
  `keep-auto-snapshots`). The cron entry and the `/opt/weaves/singlevm-backup.sh` path (different from
  the repo's `backup.sh`) **cannot be verified from code**; an operator must read crontab on the VM.
- Deploys also run it: `apply_release.py:108-109` runs `backup.sh` before each backend release (a failed
  backup aborts the release). Good, but it is the same on-host copy.
- Not backed up: `runtime.env` (DB password, `WEAVES_SECRET_ENCRYPTION_KEY`, session and OAuth secrets), Caddy certificates. A restored dump without the matching
  Fernet key cannot decrypt tenant secrets; README `:359-366` already notes key versions 1 and 2.
- Off-host copies that exist: disk snapshots (same project, 7 days, crash-consistent, not CMEK) and
  `gs://weaves-510819-migration-backups` (README `:359-361`, historical exports, no documented lifecycle
  or access review, "continue to incur small storage charges").
- The VM has no service account (`provision.sh:21` `--no-service-account --no-scopes`;
  `README.md:11`), so it **cannot upload to any bucket today**. This shapes slice 6.1.

### 1.2 Restore
- `check_backup.sh` restores a dump into a disposable database on the live Postgres and
  `verify_restore.py:13-31` compares sorted-COPY-row SHA-256 fingerprints for every table. It is manual,
  unscheduled, unalerted, and tests the copy on the same host. A restore of a fresh backup matched row
  for row on 2026-11-21 (`STATUS_AND_BACKLOG.md`), and the 2026-11-15 cutover did one. There is **no
  procedure or tool for restoring one tenant**, none for whole-VM disaster recovery from off-host
  data, and no recorded recovery time or data-loss window (RPO today is up to 24 h).
- Deletion: `README.md:222-224` states backups taken before deletion still contain the data, "needing
  the phase 6 retention procedure". `docs/DATA_MODEL.md:220` keeps an `organizations` tombstone row, but
  a backup older than the deletion simply contains the live rows; nothing records deletions outside the
  database, so restoring would silently resurrect the tenant. The same holds for revoked
  `api_credentials` (digest + status, `DATA_MODEL.md:226`) and `account_sessions` (`:208`, 12 h life).

### 1.3 Deploys, rollback, compatibility
- Deploy is automatic on merge (`.github/workflows/ci-deploy.yml:118-180`). The image is built with a
  commit-SHA **tag** (`:161`) but deployed by **digest** from the build output (`:167`);
  `release_runtime.py:67-71` accepts only `...backend@sha256:<64 hex>` in this project's registry.
- Rollback today: automatic only when activation fails (`apply_release.py:115-120` restores the previous
  `runtime.env` and config). Manual rollback means re-running `deploy.py` with an old digest the operator
  already knows (README `:70-80`). **Digest history is not kept**: `apply_release.py:124-127` overwrites
  `releases.json` per component with only the latest `{image, revision}`; the previous digest survives
  only in GitHub run logs. There is no "minimum supported rollback revision" and no check that an old
  image can read the current schema. README `:61-66` simply says rollback past a new schema is
  unsupported (migration 22 deleted data), which is a statement, not a gate.
- The real-stack tenancy job runs **only on pull requests** (`ci-deploy.yml:54`, `:119-120` tolerates
  `skipped`), against the counterpart pinned in `tenancy_harness/pair.json` (frontend 6a1edb5..., backend
  88848d2... and `phase: 4`, already behind). Frontend merges change the counterpart; re-pinning is a
  manual backlog item. Evidence artifacts live 7 days (`:116`). There is no compatibility manifest and
  nothing exercises an old image against the new schema.

### 1.4 Staging, alerting, kill switch, operators
- **Staging: none** (`docs/HANDOFF.md` section 2 says so). The harness (`tenancy_harness/`: real backend
  image, native worker, PostgreSQL 16, real frontend, fake Google, fake models, `compose.yml`) is the
  substitute; it covers T0 to T5 isolation, lifecycle, limits, usage, bounded queries and correlation, and
  the live-AI stage through the budget relay. It has **no** backup/restore, rollback, upgrade-with-data,
  crash-database, or two-image test.
- **Alerting: none** (no uptime/alert config anywhere under `infra/` or `.github/`). Metrics exist
  (`apps/api/metrics.py:1-9`, `apps/worker/metrics_server.py`, queue gauges
  `infrastructure/database/queue_metrics.py`) but are Docker-network-only (Caddy returns 404 for
  `/api/v0/internal/*`, `Caddyfile:11-16`) and nothing scrapes them. Post-deploy checks read only
  `/api/v0/health` (`ci-deploy.yml:180`). Branch protection on `main` is off ("Branch not protected").
- **Disabling a tenant today**: `organization_lifecycle.py` suspend/resume/request-deletion/purge-now
  with audit events (README `:187-226`); per-org caps via `organization_limits.py`;
  `WEAVES_ADMISSIONS_PAUSED` and `WEAVES_LIMITS_MODE` (`compose.yaml:14-15`) which need a `runtime.env`
  edit and a container restart; `issue_account_session.py` refuses disabled accounts. Gaps: suspension
  does not revoke API credentials or cancel schedules/webhooks/connector tokens as one action; there is no
  runtime global read-only or signup switch; operator scripts need SSH and have no shared durable
  operator-action log (waitlist actions are log-line only, README `:303-306`).
- Operator scripts live in `infra/gcp/singlevm/` (`bootstrap_account_owner.py`, `issue_account_session.py`,
  `organization_lifecycle.py`, `organization_limits.py`, `usage_report.py`, `waitlist.py`,
  `backfill_schedule_timezones.py`); they are streamed into the API container and use the owner DB
  connection (RLS bypass). SSH is IAP-only; OS Admin Login scoped to the VM.
- Public signup is gated by `WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION`, the allowlist variables and
  `WEAVES_PUBLIC_REGISTRATION=false` (`compose.yaml:106`); README warns that with no allowlist set anyone
  with a verified Google email can create an account.

### 1.5 Five most important findings
1. All backups are plaintext and on the VM or in the same project; no off-host, encrypted, access-
   controlled copy of the database exists, and `runtime.env` (the decryption key) is in no backup.
2. A restore cannot be made safe today: deletions and credential revocations leave no record outside the
   database, so an older backup resurrects deleted tenants and revoked credentials.
3. Rollback has no safety net: `releases.json` forgets previous digests, no minimum rollback revision,
   no old-image-vs-new-schema check, and the real-stack job does not run on the merge commit.
4. No alerting or scraping exists; a dead VM, failed backup or stuck queue is noticed only by a person.
5. The VM has no service account, so every off-host design (uploads, Ops Agent, alerts) needs an owner
   decision on identity; the snapshot policy and cron are VM state not defined in code.

## 2. Design principles
- Whole-service disaster recovery (new VM from off-host data, hours, whole DB) and tenant recovery
  (one org into a running prod, per-row conflict rules) are separate tools with separate runbooks.
- Tombstones must live **outside** the database being restored (a tombstone inside an old backup is
  missing exactly when needed). The restore path replays them before any service starts.
- The VM only gets write-only, append-only capability to off-host storage; reading/deleting backups
  needs a separate human identity. A compromised VM must not be able to erase history.
- Drills run on a disposable VM/DB without outbound network, never in place on production.

## 3. Slice plan
Order: 6.0, then 6.1 to 6.8 in parallel groups, then 6.9, then 6.10. "Files" are new unless marked.
### 6.0 Release ledger and digest retention (small, first)
- Goal: keep every deployed digest and the revision pair; define minimum supported rollback revision.
- Files: `infra/gcp/singlevm/release_runtime.py` and `apply_release.py` (append `releases.jsonl` with
  component, image, revision, schema head, time, result; keep `releases.json` semantics); repo file
  `infra/gcp/singlevm/release_policy.json` (`min_rollback_revision`, registry retention N); `ci-deploy.yml`
  prints the manifest line; tests in `tests/contracts/test_singlevm_release.py`.
- Migration: no. Tests: unit (append-only, atomic, rollback leaves ledger honest), contract (policy schema).
- Parallel: serialize with 6.7 (`apply_release.py`); disjoint from 6.1 to 6.6.

### 6.1 Off-host encrypted backup pipeline
- Goal: nightly (plus pre-release) dump encrypted client-side on the VM with an `age` public key, uploaded
  write-only to a private versioned bucket; sidecar manifest (sha256, size, schema head, git revisions,
  Fernet key version id); retention verification; separate escrow of `runtime.env`/Fernet key.
- Files: `infra/gcp/singlevm/backup_offhost.sh` and `backup_manifest.py`; `retention_check.py`
  (lists bucket, asserts freshness, count, oldest-age, versioning/lifecycle config, no public ACL);
  `infra/gcp/backup/provision.sh` (bucket, lifecycle, SA, IAM; human-run, idempotent, `--dry-run`);
  `backup.sh` keeps local dump and calls the uploader after `gzip -t`; `compose`/cron unit committed
  (systemd timer under `infra/gcp/singlevm/systemd/`) so schedule is code, not VM folklore.
- Migration: no. Tests: unit (manifest, retention math, refuses plaintext upload, refuses upload on
  failed `gzip -t`); script tests with a fake `gcloud`/local directory backend; rehearsal against a
  throwaway bucket in a scratch project, never the production bucket. Decrypt-and-verify using
  `verify_restore.py` is an acceptance test.
- Depends: owner creates bucket/SA/key (section 4). Parallel with 6.2, 6.4, 6.5, 6.6 (disjoint files).

### 6.2 Deletion and revocation tombstones with restore reconciliation
- Goal: restoring any backup never resurrects a deleted org or reactivates a revoked credential/session.
- Design: migration **29** adds `recovery_tombstones` (global, append-only via trigger that rejects
  UPDATE/DELETE, no tenant content): `kind` (`organization_deleted`, `credential_revoked`,
  `account_disabled`), `org_id`, `subject_digest` (sha-256 of the token digest, not the token), `at`,
  `shipped_at`. Written in the same transaction as lifecycle `finish_deletion`
  (`modules/auth/lifecycle_state.py`) and credential/account revocation paths. `ship_tombstones.py`
  appends them to an off-host, append-only object log (same write-only SA) and sets `shipped_at`;
  deletion reaches DELETED only after its tombstone is shipped when `WEAVES_TOMBSTONE_SHIP=required`
  (default `off`, then `observe`). Also add org-deletion and revocation rows to every backup manifest.
- Restore side: `restore_reconcile.py` (runs against the restored DB before API/worker start): replays the
  tombstone log (re-runs the existing purge for deleted orgs, marks credentials revoked, invalidates all
  account sessions), refuses to proceed if the log is unreachable or older than the backup, requires a new
  `WEAVES_BROWSER_SESSION_SECRET`, and writes a reconciliation report.
- Files: `infrastructure/database/tombstone_schema.py`, `modules/recovery/tombstones.py`, hooks in
  lifecycle/credential code (touch shared files: serialize behind other lifecycle work), scripts above,
  `scripts/export_data_model.py` regeneration, `tests/`. Also backfill from existing `organizations` DELETED rows.
- Tests: unit; PostgreSQL (trigger blocks mutation, same-transaction write, restricted role), harness
  control `T6-I01`: delete org, take dump, restore into disposable DB, reconcile, assert org unreachable,
  credential refused, sessions invalid, other tenant data intact.
- Migration: **yes (29)**. Parallel: with 6.1, 6.5, 6.6; conflicts with 6.4 (credential/lifecycle code); run before 6.3.

### 6.3 Tenant-selective export and restore into an isolated recovery environment
- Goal: restore the shared backup into an isolated disposable stack, extract one org's authorized closure,
  validate, and import into production with explicit conflict rules.
- Design: `recovery/` compose (own project, internal network only, no worker, no connectors, no model
  egress, own Postgres, secrets key mounted read-only). `tenant_extract.py` selects `product_records`
  rows by `org_id` plus the non-collection tables per `tenancy_harness/ownership.json` and the existing
  export rules (`modules/auth/organization_export_rules.py`), producing a signed bundle with row counts and
  digests. `tenant_import.py` runs on the VM as the operator in dry-run first: insert-only for IDs absent
  in production, **never overwrite** a row that exists, report conflicts (newer production wins by
  default), skip a tenant listed in tombstones, import credentials, sessions, OAuth tokens and webhooks
  as revoked/disabled so customers re-issue them, preserve ids and immutable versions, run under RLS with
  the target org bound, one transaction per batch, resumable by checkpoint, audit event
  `organization.recovered`, optional hold in SUSPENDED until the owner confirms.
- Files: `infra/gcp/recovery/compose.yaml`, `infra/gcp/singlevm/tenant_extract.py`, `tenant_import.py`,
  `modules/recovery/closure.py`, tests. Uses the existing exporter; no schema change expected (if an import
  marker is needed, fold it into migration 29 to avoid a second one).
- Tests: unit (closure vs `ownership.json`: unclassified collection fails), PostgreSQL (conflicts, RLS,
  resume, foreign rows untouched), harness `T6-I02` (damage A, restore A, B's newer writes unchanged).
- Migration: no (or shares 29). Depends on 6.1 (backup format) and 6.2 (tombstone check). Parallel with 6.5, 6.6.

### 6.4 Tenant kill switch and audited operator procedures
- Goal: one command or toggle quarantines a tenant or the platform in seconds, without a restart, with a
  durable audit trail.
- Design: migration **30** adds `platform_controls` (single-row, versioned: `admissions_paused`,
  `read_only`, `signup_enabled`, `worker_paused`; read through a 5 s cache; env values become defaults)
  and `operator_actions` (append-only: operator, command, target, reason, ticket, result). New
  `org quarantine` subcommand in `organization_lifecycle.py`: suspend, revoke that org's API credentials
  and account sessions, cancel queued work, disable schedules/webhook triggers/connector tokens;
  `release-quarantine` reverses only what quarantine did (recorded). `platform_control.py` toggles global
  flags with reason. All scripts write `operator_actions`, plus a shared `operator_audit.py` helper so
  `waitlist.py`/`organization_limits.py` can adopt it; a one-person-approval option is out of scope.
- Files: `infrastructure/database/controls_schema.py`, `modules/platform/controls.py`, hooks in admission
  (limits) and worker claim, `infra/gcp/singlevm/platform_control.py`, `operator_audit.py`,
  `organization_lifecycle.py` (edit). Frontend: a neutral "temporarily unavailable" banner copy only.
- Tests: unit; PostgreSQL (flag read-through, append-only audit, restricted role); harness `T6-I03`
  (quarantine A mid-run: no new work, in-flight stops at boundary, B unaffected, resume restores) and
  permission-denial cases (customer Admin cannot call operator paths).
- Migration: **yes (30)**. Parallel: not with 6.2 (shared lifecycle/credential files); sequence 6.2 then 6.4,
  or assign migration numbers up front (29 = 6.2, 30 = 6.4) and merge in that order.

### 6.5 Alerts and watchdog
- Goal: a human is told within minutes of: site/health down, backup older than 26 h or failing
  verification, restore drill overdue, disk over 80 %, certificate within 14 days, worker not heartbeating,
  queue age or 5xx rate over threshold, deploy failure, limit/limits-observe spike, tombstone shipping lag.
- Design: alert policies as code in `infra/gcp/alerts/*.yaml` with an idempotent `apply.sh` (human-run).
  External uptime check on `https://api.weave-studio.in/api/v0/health` and the app host. For internal
  metrics, add Ops Agent (or a small `watchdog` Compose service) scraping the Docker-network metrics and
  writing backup/heartbeat metrics; the backup uploader writes a heartbeat object/metric. Notification
  channel is owner-chosen.
- Files: `infra/gcp/alerts/`, `infra/gcp/singlevm/watchdog.py` (+ compose service, optional), thresholds
  doc in the runbook. Reads, never alters, tenant data; metrics already exclude tenant ids.
- Tests: unit (threshold logic with fake metric feed), alert-rule lint (schema), a deliberate-failure
  drill on the staging VM (stop worker, age a backup) that must page the test channel.
- Migration: no. Parallel with 6.1 to 6.4; needs 6.1's heartbeat naming.

### 6.6 Staging definition as code
- Goal: a production-shaped, disposable environment for every release candidate: PostgreSQL 16, API, worker,
  frontend, Caddy with a staging hostname, fake Google, fake model provider, fake connectors, two tenants and
  multiple roles; CI-reproducible and also creatable as a VM.
- Design: parametrize `infra/gcp/singlevm/compose.yaml` via an overlay `compose.staging.yaml` (fakes from
  `tenancy_harness`); `infra/gcp/staging/provision.sh` / `destroy.sh` create the VM from the same image
  family (create-on-demand, destroy after the gate; no persistent data, synthetic seed only, separate
  project preferred or at least a separate network, no prod secrets, its own Fernet key). Same
  `apply_release.py` path is used so the deploy tooling is itself rehearsed.
- Files: `infra/gcp/staging/*`, `infra/gcp/singlevm/compose.staging.yaml`, `tenancy_harness` new mode
  `--target staging-url` (smoke only), workflow `staging.yml` (manual dispatch first).
- Tests: the existing harness controls run against it; read-only smoke for production post-deploy remains
  separate (synthetic-tenant probes only, pre-authorized).
- Migration: no. Parallel with 6.1 to 6.5; 6.7 and 6.8 depend on it.

### 6.7 Compatibility checks and rollback rehearsal with real old digests
- Goal: before a release that adds a migration, prove the minimum supported rollback image still reads and
  writes the new schema, or declare it unsupported and raise `min_rollback_revision` deliberately.
- Design: `scripts/release_rehearsal.py` takes candidate and previous digests (from 6.0 ledger), builds a
  production-shaped DB (anonymized restore of a recent backup or synthetic scale seed), migrates with the
  candidate, starts the **old image** API and worker against it, runs a read/write smoke (the T0-T5
  subset), then starts the candidate again; asserts new data, sessions, references and Fernet decryption
  survive both directions. Output `release-compat.json` (both digests, schema head, result). CI deploy
  job fails closed if `infrastructure/database` changed and the manifest is absent/failed, and enforces
  `min_rollback_revision`. Stop skipping the harness on pushes to main when migrations changed
  (`ci-deploy.yml:54`), re-pin `pair.json` automatically (record, not float), and keep evidence beyond
  7 days (`:116`) by uploading it to the release ledger bucket.
- Files: `scripts/release_rehearsal.py`, `tenancy_harness/rehearsal.py`, `ci-deploy.yml`,
  `infra/gcp/singlevm/release_policy.json`, tests. Registry cleanup policy: keep last N plus every digest
  in the ledger (owner approval to enable deletion).
- Migration: no. Depends on 6.0 (and 6.6 for VM-level rehearsal; CI-level runs in Actions without a VM).
  Touches `apply_release.py` and `ci-deploy.yml`: not parallel with 6.0.

### 6.8 Disaster-recovery drill tooling and runbooks
- Goal: measured, repeatable whole-service recovery and written procedures.
- Files: `infra/gcp/singlevm/disaster_restore.sh` (new VM from image family + latest off-host backup +
  escrowed key + tombstone replay + readiness check), `docs/runbooks/{incident,deletion-and-erasure,
  tenant-recovery,disaster-recovery,release-and-rollback,kill-switch,secrets-rotation,backup-access}.md`,
  update `infra/gcp/singlevm/README.md` and `docs/HANDOFF.md`, `MULTI_TENANCY_IMPLEMENTATION_PLAN.md` progress.
- Tests: scripted drill on staging/disposable VM with a synthetic dataset: timed restore (record RTO),
  data-loss window (RPO) from backup age; API/worker crash, database interruption, provider timeout, and
  deletion-resume cases using the harness; operator permission-denial checks.
- Migration: no. Depends on 6.1, 6.2, 6.6. Parallel with 6.4, 6.5 (docs).

### 6.9 Supported minimum capacity
- Goal: a declared minimum deployment (VM shape, disk, worker slots, per-org caps) tied to measurements.
- Files: `docs/phase-6-capacity.md`; limits defaults. Inputs: 5.6's envelope plus restore time and disk
  growth. Depends on 5.6 and 6.8. No migration.

### 6.10 Private-beta release gate
- Goal: a machine-checkable checklist `scripts/release_gate.py` run before inviting outsiders and again
  before signup is considered: every phase 0 to 5 control present and not skipped; T6 controls pass;
  last restore drill and last rollback rehearsal are younger than 30/90 days; backup freshness and
  retention check green; alerts firing test passed; kill switch tested; runbooks present; capacity doc
  matches deployed limits; `signup_enabled=false`, allowlist set, `WEAVES_PUBLIC_REGISTRATION=false`.
  The gate **reports only**; flipping public signup is a human decision and stays off.
- Files: `scripts/release_gate.py`, `docs/phase-6-gate.md`, tests; `pair.json` phase bump. Depends on all; no migration.
### 3.1 Summary table

| Slice | Migration | Depends on | Runs in parallel with |
|---|---|---|---|
| 6.0 Release ledger | no | none | 6.1, 6.2, 6.5, 6.6 |
| 6.1 Off-host encrypted backups | no | owner infra | 6.0, 6.2, 6.5, 6.6 |
| 6.2 Tombstones + reconcile | 29 | 6.1 format (loosely) | 6.0, 6.1, 6.5, 6.6 |
| 6.3 Tenant restore | no (maybe 29) | 6.1, 6.2 | 6.4, 6.5, 6.6 |
| 6.4 Kill switch + audit | 30 | after 6.2 (shared files) | 6.1, 6.3, 6.5, 6.6 |
| 6.5 Alerts | no | 6.1 heartbeat | 6.0 to 6.4, 6.6 |
| 6.6 Staging as code | no | none | 6.0 to 6.5 |
| 6.7 Compat + rollback rehearsal | no | 6.0 (+6.6) | 6.1 to 6.5 |
| 6.8 DR drill + runbooks | no | 6.1, 6.2, 6.6 | 6.4, 6.5 |
| 6.9 Min capacity | no | 5.6, 6.8 | none |
| 6.10 Release gate | no | all | none |

Per slice: PR, quality checks, real-stack harness, budget-gated live AI run (lifetime ledger), merge, let
GitHub deploy, verify. 6.2 and 6.4 carry migrations: expand-then-enable, flags default off, fresh backup first.

## 4. Who does what

### 4.1 AI-doable (code, scripts, tests, docs)
All file work in section 3 (ledger, uploader, tombstones, extract/import, kill switch, alert rules,
staging/recovery compose, rehearsal and gate scripts, runbooks, harness controls T6-I01 to T6-I03, and
`--dry-run` modes of every provisioning script), tested on local PostgreSQL and a fake bucket.

### 4.2 Needs the owner (explicit approval or credentials)

| Action | Why it needs a human | Rough monthly cost |
|---|---|---|
| Create private GCS bucket `...-backups` (versioning, soft-delete, lifecycle 35 d + monthly keepers, uniform access, public-access prevention), region `asia-south1` | New cloud spend, data-at-rest location | ASSUMPTION about 5 GB at USD 0.023/GB: under USD 0.15. Source: https://cloud.google.com/storage/pricing |
| Second bucket (or prefix with retention lock) for tombstone log and release evidence | Separate trust boundary | under USD 0.05 (same source) |
| Backup writer service account (objectCreator only) and attaching it to the VM (requires stopping the VM; a few minutes downtime) | IAM change, outage | USD 0 (IAM free). https://cloud.google.com/iam/pricing |
| Separate human "backup reader/restore" identity and break-glass access | Access control decision | USD 0 |
| Encryption: age public key on VM, private key held offline by two founders (no cloud cost). Optional CMEK via Cloud KMS | Key custody | age: USD 0. KMS ASSUMPTION USD 0.06 per key version plus about USD 0.03 per 10k operations, so about USD 0.1. https://cloud.google.com/kms/pricing |
| Escrow of `runtime.env`/Fernet key (Secret Manager or offline paper/vault) | Holds production secrets | Secret Manager ASSUMPTION about USD 0.06 per active version plus access fees, under USD 0.2. https://cloud.google.com/secret-manager/pricing |
| Enable deletion of old backups (bucket lifecycle) and shortening old snapshot/bucket retention, including `weaves-510819-migration-backups` | Irreversible data deletion; erasure policy | saves money; ASSUMPTION |
| Artifact Registry cleanup policy keeping ledger digests | Irreversible deletion of images | ASSUMPTION 10 to 20 GB retained at USD 0.10/GB beyond free tier, about USD 1 to 2. https://cloud.google.com/artifact-registry/pricing |
| Restore drill using real production data (even into an isolated VM) | Customer-adjacent data leaves prod; time | covered by staging costs below |
| Staging VM: e2-medium in asia-south1 with 30 GB disk, **created per gate and destroyed** (about 8 h/month) | Cost, network/IAM scope | ASSUMPTION: always-on about USD 30 to 35/month (VM about USD 27 plus disk about USD 4), on-demand 8 h about USD 0.5. https://cloud.google.com/compute/vm-instance-pricing, https://cloud.google.com/compute/disks-image-pricing |
| Reserved static IP for staging (only if always-on; unattached IPs bill) | Cost | ASSUMPTION about USD 7 unattached. https://cloud.google.com/vpc/network-pricing |
| Recovery drill VM (same shape, 4 h per quarter) | Cost | under USD 1/month averaged |
| Alerts: Cloud Monitoring uptime checks, alert policies, Ops Agent (needs VM service account roles `monitoring.metricWriter`, `logging.logWriter`) | IAM, channel contact | ASSUMPTION USD 0 within free allotments (uptime checks and basic policies); log/metric ingestion above free tier is small. https://cloud.google.com/stackdriver/pricing |
| Alert channels: owner's email, SMS or phone (phone numbers are personal data), optional third-party pager | Personal contact details | email USD 0; SMS/pager ASSUMPTION USD 0 to 20 |
| Additional snapshot (pre-drill) or longer snapshot retention | Cost | ASSUMPTION about USD 0.03/GB-month of changed data, under USD 3 |
| GitHub: branch protection and required checks on `main`, secrets/variables for new workflows | Repo admin | USD 0 |
| Flipping public signup or inviting outsiders | Business/legal decision | n/a |

Steady state without always-on staging: about USD 2 to 5 per month; with it, about USD 35 to 45. Recommend on-demand staging.

## 5. Risks and safe rollout per slice

| Slice | Main risk | Mitigation and rollout |
|---|---|---|
| 6.0 | Ledger write fails and aborts a good release | Best-effort append after health check; failure warns, never rolls back. |
| 6.1 | Backup upload breaks the existing nightly local dump; key loss makes backups unreadable; VM SA stop causes downtime | Local dump stays the first step; upload failure alerts but does not delete local copy; test decrypt on a second machine before trusting; schedule the SA attachment in a quiet window; key custody before first upload; start with a scratch bucket. |
| 6.2 | A hook in the lifecycle path blocks deletion; shipping outage stalls purge; wrong replay deletes live data | Modes `off`, `observe`, `required`; replay only touches rows named by tombstone; dry-run report first; reconcile refuses when the log is older than the backup; migration is additive and the trigger is on one new table. |
| 6.3 | Import overwrites newer data or leaks across tenants; revived credentials | Insert-only, dry-run default, org-bound RLS transaction, credentials imported revoked, tombstone check, bundle digest verified, restore only into an isolated stack with no egress. |
| 6.4 | Runtime flag read adds a query on the hot path or makes a wrong global lock-out | 5 s cache, fail-open to env defaults for reads but fail-closed for `read_only` when explicit; quarantine records exactly what it changed so release is exact; break-glass is the existing SSH script path. |
| 6.5 | Alert noise or silent gaps; SA with monitoring roles widens the VM's trust | Thresholds reviewed after one week; deliberate-failure drill; minimal roles; no tenant data in metrics. |
| 6.6 | Staging leaks prod secrets/data or runs forever (cost) | Own Fernet key and project/network, synthetic data only, auto-destroy label and TTL, no outbound network except the fakes. |
| 6.7 | Rehearsal passes on synthetic data but not production shape; gate blocks urgent fixes | Production-shaped seed plus restored backup variant; documented, logged override requiring owner approval; first release only reports. |
| 6.8 | A drill touches production | Drill scripts refuse the production project and VM name; run only in the staging/recovery environment. |
| 6.9, 6.10 | Capacity numbers copied without evidence; gate treated as approval | Numbers cite measurements; the gate prints results and never changes configuration. |

### What can be tested without touching production
Everything in the repository: unit and PostgreSQL tests on a local database; harness controls with the
real images; backup, tombstone, restore, extract/import and rehearsal flows on a local Docker host using
the harness stack and fake bucket directory; alert rule linting; the full rollback rehearsal in Actions or
on a disposable staging VM with synthetic or anonymized data. First production touchpoints, each needing
owner go-ahead: creating the buckets/SA, the VM stop for SA attachment, enabling the nightly upload, shipping
tombstones, a read-only restore into the isolated VM from the real encrypted backup, and enabling any
deletion lifecycle.

## 6. Open decisions for the owner
1. Retention target: recommended 35 days of dailies plus 12 monthly keepers, with a documented erasure
   statement (deleted tenant data may remain in backups up to 35 days, never restored without tombstone
   replay). Monthly keepers conflict with strict erasure; choose.
2. RPO/RTO targets (proposal: RPO 24 h now, 1 h with WAL archiving later; RTO under 4 h); WAL archiving
   is deliberately not in this phase.
3. Alert recipients and escalation, and who holds the two key custodian roles.
4. Staging always-on versus per-gate.
5. Whether the company project should own the backup bucket and keys (separation from the production admin identity is advisable).
