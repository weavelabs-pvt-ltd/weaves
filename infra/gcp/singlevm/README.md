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
and contains generated database/SSO secrets, the encryption key and image refs.
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
pages. They never create tasks or use the owner's password.

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
Database migrations are not reversed: migrations must remain compatible with
the previous application version. This single VM has a brief service restart;
it does not provide zero-downtime deployment or high availability. The worker
finishes its current operation on SIGTERM; work exceeding five minutes may be
recovered through the durable execution queue.

Release revisions and digests are recorded in root-only
`/opt/weaves/singlevm/releases.json`. GitHub Actions contains each deployment
result. Failed checks or deployments fail the workflow visibly.

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
with SHA-256 fingerprints, then drops only that verification database. Recovery
into the live database requires explicitly stopping API/worker, restoring into
an empty target, verifying the export and restarting the services. Preserve the
encryption key corresponding to the backup.

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
`shivamsawarn15@gmail.com` was created internally, with public onboarding kept
closed and the unused bootstrap API credential removed. The old CI account and
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
