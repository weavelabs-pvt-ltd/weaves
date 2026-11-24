# Single-VM operations

Project `weaves-510819`, account `shivamsawarn15@gmail.com`, zone
`asia-south1-a`. Specify this account/project on every gcloud command; do not
change the developer machine's globally active account.

One `e2-medium` VM, `weaves-personal`, runs PostgreSQL 16, API, worker, static
frontend and Caddy through Docker Compose project `weaves-single`. The regional
static IP is `8.231.88.4`. Only web ports 80/443 are public; SSH is restricted to
IAP. PostgreSQL has no host port. API port 8001 and frontend port 8080 bind only
to loopback for administration. The VM has no attached service account.

## Files and updates

Server configuration lives in `/opt/weaves/singlevm`. `runtime.env` is root-only
and contains the generated database secret, the encryption key and image refs.
Caddy certificates and PostgreSQL use separate persistent Docker volumes.
Logs rotate at 3 × 10 MB per container. API and worker use four database pool
connections each. `WEAVES_SEED_LOCAL_DEFAULTS=false` prevents local demo
accounts, agents and workflows from appearing after restarts. Local development
keeps its existing default example data.

## Database row-level security (opt-in)

Migration 21 creates the non-owner `weaves_app` role and tenant policies. They stay dormant until `WEAVES_DB_APP_ROLE=weaves_app` is added to `/opt/weaves/singlevm/runtime.env` and the API/worker are restarted; every transaction then runs as that role with the tenant bound transaction-locally. To roll back, remove the variable and restart (the owner connection bypasses the policies; the migration is additive).

## Automatic deployment

Every push to `main` in `weavelabs-pvt-ltd/weaves` or
`weavelabs-pvt-ltd/weaves-app` runs its quality checks and then automatically
publishes and deploys an image digest to this VM. Pull requests only verify.
The same workflow can be retried with `workflow_dispatch` on main. A newer main
commit supersedes an older release before deployment.

Backend CI runs Ruff lint/format, strict mypy, file/function size checks, all
Python tests against PostgreSQL 16, and the container build. Frontend CI runs
ESLint, JavaScript syntax, the container build and Chromium browser tests.
Post-deployment checks read the health endpoint and anonymous website/sign-in
pages. They never create tasks or use any owner credential.

Google authentication uses GitHub OIDC and Workload Identity Federation. No
service-account key or production password is stored in GitHub. The identity is
restricted to these repositories' main deployment workflows, registry writes,
IAP SSH to this VM and VM-scoped OS Admin Login. Repository variables specify
`weaves-510819`, `asia-south1`, registry `weaves`, VM `weaves-personal`, zone
`asia-south1-a`, the deploy service account and WIF provider. The company cloud
project is not a deployment target of these workflows.

The host's `apply_release.py` holds a shared `flock` through readiness and
rollback, so FE and BE repositories cannot overwrite each other's releases.
It reads the current root-only environment *after* acquiring the lock and
changes only the selected image reference. Docker authentication is temporary.
Backend releases back up PostgreSQL, update checked-in Compose/Caddy settings,
stop the worker with a five-minute grace period and start API/worker together.
Schema migrations run on API startup. Frontend releases restart only frontend.
Persistent volume names are validated and cannot change in an image release.
No database reset, seed or owner bootstrap occurs.

A failed image pull leaves the running release intact. A failed activation
restores previous image refs/configuration and restarts those services.
Database migrations are append-only and never reversed, and a migration may
remove data (migration 22 purges retired sign-in rows and indexes), so an
application rollback onto an older image is not supported once a newer schema
is applied; roll forward instead. This single VM has a brief service restart;
it does not provide zero-downtime deployment or high availability. The worker
finishes its current operation on SIGTERM; work exceeding five minutes may be
recovered through the durable execution queue.

Every release attempt is appended to the root-only, append-only ledger
`/opt/weaves/singlevm/releases.jsonl` (one JSON line each: `seq`, `time`,
`component`, `image` digest, `revision`, `schema_version` (backend migration head
read from the image, null if unknown), `result` `healthy` or `failed`, and
`action` `deploy`, `rollback` or `import`). `releases.json` still holds the latest
image per component. The first release after the ledger ships imports the
existing `releases.json` as `import` entries (schema unknown) and appends from
there; nothing is lost. GitHub Actions also prints each release line in the job
summary and keeps it as a 90-day `release-manifest` artifact. Failed checks or
deployments fail the workflow visibly.

`release_policy.json` sets `min_rollback_schema_version`: the newest forward-only
migration that breaks older readers. The minimum rollback revision is the first
healthy backend ledger entry at or above it; older or unknown-schema entries are
refused, including by a manual `deploy.py` of that digest. Raise the number
deliberately when a migration breaks older images.

```sh
sudo python3 /opt/weaves/singlevm/rollback_release.py list [--component backend]
gcloud auth print-access-token | sudo python3 /opt/weaves/singlevm/rollback_release.py \
  rollback --component backend --to SEQ_OR_DIGEST_PREFIX [--dry-run]
```

`list` marks the floor with `*`. Rollback redeploys a ledger digest through the
normal locked path (backup, health check, automatic restore on failure).

For a manual release or application rollback, use an existing immutable digest
and its corresponding commit SHA from a tested checkout:

```sh
python3 infra/gcp/singlevm/deploy.py --component backend \
  --image asia-south1-docker.pkg.dev/weaves-510819/weaves/backend@sha256:DIGEST \
  --revision COMMIT_SHA
```

Use `--component frontend` with the frontend digest to change only frontend.
This command defaults to the personal Google account above; CI passes its
federated service-account identity explicitly. Never run the historical managed
Cloud Run/SQL deployment helpers for this environment.

## Backup and restore

`/opt/weaves/singlevm-backup.sh` writes gzip SQL dumps into the root-only
`/opt/weaves/backups/singlevm` directory. Cron runs at 19:45 UTC daily with
seven-day retention. The existing daily disk snapshot policy runs at 20:00 UTC
and also retains seven days. The 30 GB disk is not auto-deleted with the VM.

Run a backup manually, then validate a specific dump without changing the live
database:

```sh
sudo /opt/weaves/singlevm-backup.sh
sudo bash /opt/weaves/singlevm/check_backup.sh /path/to/backup.sql.gz
```

The check restores into a disposable database, compares every sorted COPY row
with SHA-256 fingerprints, then drops only that verification database. Recovery into the live database requires explicitly stopping API/worker, restoring into
an empty target, verifying the export, **reconciling tombstones** and only then restarting
the services. Preserve the encryption key corresponding to the backup.

### Tombstones and restore reconciliation (Phase 6.2)

An old backup contains tenants and credentials deleted or revoked since it was taken.
Every organization purge, API-credential revocation or rotation, and secret deletion or
rotation (connector credentials, provider keys) therefore appends a tombstone to
`/opt/weaves/singlevm/tombstones/tombstones.jsonl` (mounted into the API and worker at
`/var/lib/weaves/tombstones`, `WEAVES_TOMBSTONE_DIR`). The log is outside the database, one
JSON object per line, hash-chained (`seq`, `prev`, `hash`), and holds ids, kinds and
timestamps only: no tokens, secrets, names or emails. `backup.sh` writes
`<dump>.tombstone-head` (log sequence and hash) before each dump.

Failure behaviour: an organization purge writes its tombstone before erasing anything and
again before reaching DELETED, and a secret is tombstoned before it is deleted or rotated;
if the log is unwritable the operation is refused or retried on the next worker tick, never
done silently. API-credential revocation is committed first (a security action is never
blocked) and then recorded; a failed write returns an error and repeating the revoke
records it. Rotation rolls back if it cannot be recorded. Keep the directory on durable
disk and include it in VM snapshots; until off-host shipping exists (Phase 6.1) a lost VM
disk loses the log.

Restore procedure (API and worker stopped; run from `/opt/weaves/singlevm`):

```sh
R="docker compose --env-file runtime.env run --rm --no-deps -T product-api python -"
$R mark-restore --backup /opt/weaves/backups/singlevm/weaves-X.sql.gz < restore_reconcile.py
# restore the dump into an empty database, then rotate WEAVES_BROWSER_SESSION_SECRET
$R apply --dry-run --head-file /opt/weaves/backups/singlevm/weaves-X.sql.gz.tombstone-head < restore_reconcile.py
$R apply --head-file /opt/weaves/backups/singlevm/weaves-X.sql.gz.tombstone-head < restore_reconcile.py
```

`mark-restore` writes `restore-pending-<id>.json`. While it has no matching
`restore-reconciled-<id>.json` the API and worker exit at start-up and `apply_release.py`
refuses to release. `apply` replays purges (existing purge service, to DELETED), revocations
and secret deletions, revokes every account session, writes a report and then the
completion marker. It refuses (changing nothing) when the log is corrupt, shorter than the
backup's recorded head, or no head is supplied (use `--no-head-check` only when the backup
has no sidecar; rotated-secret tombstones are then skipped and listed). It is idempotent;
rerun it with `--restore-id` after any interruption.

## Google sign-in runbook

Google is the only sign-in method; there is no email delivery on the VM. The frontend must use `WEAVES_API_BASE=/api/v0`: account cookies are host-bound to `app.weave-studio.in`, whose `/api/v0/*` routes proxy to the API. The separate API hostname remains available for service credentials, not browser-cookie calls.

1. In the Google Cloud console open APIs & Services, then OAuth consent screen.
   Choose user type External (Internal only works inside a Workspace
   organization), set the app name and support email, and add the scopes
   `openid`, `email` and `profile` (all non-sensitive). While the app is in
   Testing, add the owner's Google address under Test users; publish it to let
   other accounts sign in.
2. Under Credentials create an OAuth client ID of type Web application. Add the
   Authorized redirect URI
   `https://app.weave-studio.in/api/v0/account/google/callback` (exactly, no
   trailing slash; the app and API share the `app.weave-studio.in` origin
   through the reverse proxy). No JavaScript origin is needed. Copy the client
   ID and secret.
3. On the VM add the values to `/opt/weaves/singlevm/runtime.env` (root only,
   mode 0600; never commit them) and recreate the API:

   ```
   WEAVES_GOOGLE_CLIENT_ID=<client id>
   WEAVES_GOOGLE_CLIENT_SECRET=<client secret>
   WEAVES_ACCOUNT_ALLOWED_EMAILS=owner@example.com
   WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION=true
   WEAVES_ACCOUNT_ORGANIZATION_LIMIT=3
   # optional: WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS=example.com
   # optional: WEAVES_GOOGLE_DISCOVERY_URL (tests only)
   ```

   `WEAVES_BROWSER_SESSION_SECRET` (at least 32 bytes) must also be set. Without
   the client ID and secret the Google routes return 404. **If neither allowlist
   variable is set, anyone with a verified Google email can sign in and creates
   an account**, so keep `WEAVES_ACCOUNT_ALLOWED_EMAILS` set on this VM. Values
   are read once at API start; restart the API after changing them.
4. Bootstrap the owner (below) with the owner's Google email, then sign in at
   `https://app.weave-studio.in`. The first Google sign-in finds the bootstrapped
   account by its verified email, adds the Google identity to it and lands in the
   existing organization.
5. Break-glass: if Google is unreachable or misconfigured,
   `issue_account_session.py` (below) issues a short account bearer session for
   the owner's existing account; use it with `X-Organization-Id`.

## Connectors

GitHub, Slack, Jira, Notion and Datadog need provider apps and runtime settings; see [CONNECTORS.md](CONNECTORS.md).

## Operator account bootstrap

Two operator-only scripts create and recover access. They open the database
owner's connection (`app_role=''`, so tenant row policies do not apply), refuse
to run with local demo seeding, are never reachable over HTTP, and print one
structured, secret-free JSON log line to stderr. They are not part of the image;
stream them into the running API container from
`/opt/weaves/backend/infra/gcp/singlevm`:

```
docker compose exec -T product-api python - \
  --email owner@example.com --organization-name "Weaves" \
  < bootstrap_account_owner.py
docker compose exec -T product-api python - \
  --email owner@example.com --minutes 60 < issue_account_session.py
```

`bootstrap_account_owner.py` is idempotent: it creates the Account, a VERIFIED
email identity (use the owner's Google email), the organization through
`provision_organization` (ACTIVE owner, no API credential) and the membership,
and re-running it changes nothing. It first links the verified operator email to existing tenant users/principals, then reuses an organization of that name the account already belongs to; a cutover must pass the existing organization name to preserve it. When that person signs in with Google the verified
email matches, so they are the same account in the same organization.
`issue_account_session.py` is break-glass: it issues an account
bearer session for an existing active account (`--minutes` 1 to 1440, default
60) and prints the token once on stdout; unknown and disabled accounts are
refused. Use it with `X-Organization-Id: <organization id>` against the API.

## Organization lifecycle (operator)

`organization_lifecycle.py` is the only way to suspend, resume or delete an
organization on behalf of the platform. Like the scripts above it opens the
database owner's connection, refuses to run with local demo seeding, is never
reachable over HTTP and is streamed into the running API container (the owner
cannot resume a suspension; customer Admins have no operator authority):

```
docker compose exec -T product-api python - \
  suspend --organization-id ORG --operator you@example.com \
  --reason "billing hold" < organization_lifecycle.py
docker compose exec -T product-api python - \
  status --organization-id ORG < organization_lifecycle.py
docker compose exec -T product-api python - list < organization_lifecycle.py
```

Subcommands: `suspend`, `resume`, `request-deletion`, `cancel-deletion`,
`purge-now` (also needs `--confirm-organization-name "<exact name>"`), `status`
and `list`. Every action needs `--operator` (your address) and `--reason`, writes
an `organization.lifecycle.*` audit event attributed to the operator
(`actor_kind` `operator`) and one secret-free JSON log line; the result is JSON on
stdout. Exit status 2 means the action was refused (unknown organization,
disallowed transition, wrong confirmation). Repeating an action is a no-op.

* Suspension stops all new work at the next boundary and refuses every request
  for that organization with `403 Organization not available`; other organizations
  of the same accounts are unaffected. Resume never replays missed schedules,
  cancelled jobs or old webhook events.
* `request-deletion` starts the grace period
  (`WEAVES_ORGANIZATION_DELETION_GRACE_DAYS`, default 7). While pending, only the
  owner can read the state, export, and (for owner-requested deletions) cancel.
  An operator-requested deletion can be cancelled only with `cancel-deletion`,
  which restores the state it was requested from.
* After the grace period (or `purge-now`) the worker deletes the organization in
  bounded steps every `WEAVES_LIFECYCLE_POLL_SECONDS` (default 30). `status` shows
  the state, the last completed step, rows deleted and `remaining_rows`. A crash
  or restart resumes safely. DELETED is final: it cannot be resumed, selected or
  re-created. Retained: the organization tombstone, its lifecycle history, the
  `organization.lifecycle.*` and `organization.exported` audit events and the
  account creation-quota ledger (a deleted organization still counts toward the
  account's cap). Everything else, including other audit events and secrets, is
  removed. Backups taken before deletion still contain the data; restoring one
  needs the phase 6 retention procedure.
* Time-limited support access is not provided: there is no operator HTTP surface.
## Tenant limits (operator)

Per-organization limits (queued work, tasks, seats, schedules, agents, workflows, tool calls per day and request
rate) are checked at admission, atomically with the insert, and `GET /api/v0/organization/limits` shows an
organization's own usage. `WEAVES_LIMITS_MODE` in `runtime.env` decides what a breach does: `observe` (the
default) logs `limit would be exceeded (observe mode)` and changes nothing, `enforce` answers HTTP 429
`limit_exceeded`, `off` disables the checks. Read the log for a deploy before switching to `enforce`.
`WEAVES_ADMISSIONS_PAUSED=true` refuses new work only (reads and cancellation keep working). Roll back by
setting the mode back to `observe` or `off` and restarting; no migration is undone.
`organization_limits.py` raises or lowers one organization's caps (it takes effect within about 30 seconds):

```
docker compose exec -T product-api python - \
  set --organization-id ORG --operator you@example.com --reason "pilot" --seats 60 --max-queued 200 < organization_limits.py
docker compose exec -T product-api python - show --organization-id ORG < organization_limits.py
docker compose exec -T product-api python - \
  clear --organization-id ORG --operator you@example.com --reason "pilot ended" < organization_limits.py
docker compose exec -T product-api python - list < organization_limits.py
```


## AI usage and cost per organization (operator)

Every model call is recorded in a per-organization usage ledger (tokens as reported by the
provider, estimated cost in integer nano-USD from the model profile's per-million rates).
Customers use their own provider keys, so this is what they spend, not a charge by Weaves.
`usage_report.py` is the business view. Like the other scripts it opens the database owner's
connection, refuses local demo seeding, is never reachable over HTTP and is streamed into the
running API container:

```
docker compose exec -T product-api python - --by month < usage_report.py
docker compose exec -T product-api python - --by day --from 2026-11-01 --to 2026-11-30 < usage_report.py
docker compose exec -T product-api python - --by month --org ORG_ID --csv < usage_report.py > usage.csv
```

`--by day|month` (default month), `--from` / `--to` (UTC dates, inclusive; default: this month so
far), `--org` and `--csv` (JSON by default). One row per organization and period with `calls`,
`input_tokens`, `output_tokens`, `total_tokens`, `estimated_cost_usd` (exact decimal; missing for
calls whose model has no price, counted in `unpriced_calls`) and `unknown_calls` (requests that
may have reached the provider with no recorded token count, for example a worker killed
mid-call). Only counts and amounts are read, never prompts or outputs. Usage is deleted together
with its organization; for live organizations it is kept `WEAVES_USAGE_RETENTION_DAYS` days
(default 400). Organization Owners and Admins see their own numbers at `GET /api/v0/usage`.

## Waitlist sign-ups (operator)

The website's waitlist is public and only collects data (an email, consent and
up to six optional answers). `waitlist.py` is how founders read, export, report
on and erase it. Like the other scripts it opens the database owner's
connection, refuses to run with local demo seeding, is never reachable over HTTP
and is streamed into the running API container:

```
docker compose exec -T product-api python - list --status new < waitlist.py
docker compose exec -T product-api python - list --answer company_location=europe < waitlist.py
docker compose exec -T product-api python - export --csv --operator you@example.com < waitlist.py > waitlist.csv
docker compose exec -T product-api python - stats < waitlist.py
docker compose exec -T product-api python - \
  set-status --email a@b.com --status contacted --operator you@example.com < waitlist.py
docker compose exec -T product-api python - \
  note --email a@b.com --text "met at an event" --operator you@example.com < waitlist.py
docker compose exec -T product-api python - \
  delete --email a@b.com --operator you@example.com < waitlist.py
```

`list` and `export` take `--status`, `--domain-type company|public`, `--since` and
`--until` (UTC dates, on the first sign-up) and `--answer QUESTION=OPTION`; reports
use each email's latest submission. `stats` prints sign-ups per day, completion
and answer distribution per question and the company versus public share.
`delete` erases the entry and every submission (right to erasure). Every action
writes one secret-free JSON log line with `actor_kind` `operator`; `set-status` and
`note` also append to the entry's own history. Resume tokens are stored only as
digests and are never printed. CSV cells starting with `=`, `+`, `-` or `@` are
quoted so a spreadsheet does not run them. Exit status 2 means refused.

* Public routes: Caddy sends only `weave-studio.in/api/v0/waitlist/*` (and
  `www.`) to the API; every other API path stays on `app.weave-studio.in`. The
  website calls a relative URL, so there is no CORS or cookie involvement.
* Client address: Caddy sets `X-Forwarded-For` to the real peer and uvicorn runs
  with `--proxy-headers`, so the limiter sees that address. It is used only as a
  hashed, short-lived rate-limit bucket (join 10/hour per address and 5/hour
  per email, answers 60/hour per token). No address is stored.
* Retention: the worker deletes submissions not updated for
  `WEAVES_WAITLIST_RETENTION_DAYS` (default 365) and entries left with none,
  hourly. A failure is logged and never stops the worker.
* Question sets are repository files (`contracts/waitlist/questions/v<N>.json`,
  pinned in `published.lock.json`); changing them is a reviewed pull request that
  adds a new version, never an edit. After deploy, check `GET /api/v0/health`,
  that migration 24 is recorded, then submit one test join and delete it with
  `delete`.

## Schedule timezone backfill

Records without a `timezone` already behave as `Asia/Kolkata`, so this step is
optional and only makes that explicit in storage. It uses the owner connection
like the other operator scripts and prints counts only:

```
docker compose exec -T product-api python - < backfill_schedule_timezones.py          # dry run
docker compose exec -T product-api python - --apply < backfill_schedule_timezones.py
```

The default is a dry run. `--apply` writes `timezone='Asia/Kolkata'` into
interval schedules and calendar settings that have none; records that already
name a zone are never touched, `updated_at` and `next_run_at` are preserved and
re-running changes nothing. The two flags are mutually exclusive.

## Migration and explicit reset record

The source Cloud SQL export was restored and matched all 182 application rows,
18 migration rows, login throttling and secret-table rows. Real owner login,
worker task execution/transcript and all main pages passed on the VM before
cutover. DNS moved from the temporary global load balancer to the VM and all
four hostnames passed normal HTTPS checks. The managed API was frozen and its
worker stopped during the final export, avoiding two writable databases.

The owner then explicitly authorized deleting Cloud SQL and clearing all VM
application data. Both old database volumes were removed; PostgreSQL initialized
a fresh cluster and applied versions 1–18 at 2026-11-15 17:44:30 UTC. A sole owner
`shivamsawarn15@gmail.com` was created internally (at that time with a
password; the password, public onboarding and tenant sign-in routes have since
been removed; Google sign-in is now the only method). The old CI account and
all prior agents/workflows/tasks/skills are absent. Required system defaults
remain. Fresh owner details are in `~/.config/weaves-public/login.json` locally,
mode 0600. The old sign-in credentials are no longer valid.

The fresh backup passed an independent restore/fingerprint check. Restarting
API/worker preserved the clean state. The backend's 288 tests, including real
PostgreSQL tests, strict mypy, Ruff lint/format and size checks passed. Browser
login and all seven main pages passed against the fresh VM with no JS or failed
HTTP responses.

Historical recovery data remains **off the VM**:

- Private bucket `gs://weaves-510819-migration-backups` contains initial/final
  source exports and `vm-before-reset-20261115.sql.gz`.
- Snapshot `weaves-personal-before-public-20261115` preserves the former private
  testing installation. It does not represent the current live database.
- Historical exports use `weaves-fernet-key` version 1; the fresh runtime uses
  version 2. Retain the old version for recovery of archived encrypted records.

Old VM-local dumps, the private testing deployment, obsolete database volumes
and unused container images were removed. HTTPS certificates and current
configuration were preserved. These off-VM archives intentionally contain old
data and continue to incur small storage charges.

Final cloud inventory: one running VM and its reserved regional IP; no Cloud SQL
instances, Cloud Run services/worker pools, forwarding rules or backend services.
The regular `deploy.py` update path was exercised with the current image refs
and preserved the owner and empty work lists. All four public HTTPS hostnames
returned 200 directly from `8.231.88.4`; HTTP redirected to HTTPS with 308.
Snapshot `weaves-single-clean-20261115` records the fresh installation separately
from the historical archive.

## Compatibility check and rollback rehearsal

`scripts/release_rehearsal.py run` proves, before a release that touches
`infrastructure/database`, that the oldest supported rollback image (the commit
pinned as `min_rollback_revision` in `release_policy.json`) still works on the
upgraded schema. On a throwaway local Docker stack (own network, PostgreSQL,
generated key; no cloud, registry or production access) it seeds two tenants
with the old image, upgrades to the candidate, writes new data, rolls back to
the old image, writes again, and rolls forward. It checks that data, workflow to
agent references, account sessions, tenant isolation and Fernet-encrypted
secrets survive every swap, and that API and worker stay up. The old image is
built with `git archive <min_rollback_revision>` (or `--old-image`). The result
is `release-compat.json`; `release_rehearsal.py verify` fails closed on a
missing, failed, dirty or wrong-revision manifest. CI runs it, and the real-stack
tenancy suite, on pushes to main only when the schema or the rehearsal changed,
and the deploy job requires both. Raising `min_rollback_schema_version` means
raising `min_rollback_revision` in the same change.
