# Personal testing deployment

This private IAP-only setup is retired. The same VM now runs the public
[single-VM deployment](../singlevm/README.md). Its former database volume and
private source/configuration were removed after the explicit fresh reset.
The remaining sections describe the historical private setup.

Historical setup used project `weaves-510819` and account
`shivamsawarn15@gmail.com`. Current GitHub deployment variables target the public
single VM; follow the linked operations guide.

One `e2-medium` VM in `asia-south1-a` runs PostgreSQL, API, worker and frontend
with Docker Compose. A 30 GB balanced persistent boot disk is retained on VM
deletion. Daily disk snapshots and daily compressed PostgreSQL dumps retain
seven days. Container logs rotate at 3 × 10 MB per container. Single-VM hosting
has no automatic failover; it is intended for personal testing.

## Access

Run `bash infra/gcp/personal/tunnel.sh`, then open `http://localhost:18080`.
Keep the tunnel running while using the application. Only port 22 from Google's
IAP network is allowed through the VM firewall. The app is bound to loopback;
PostgreSQL and the API have no host ports. The VM's external IP provides outbound
internet access; it is not a public application endpoint. No load balancer,
Cloud NAT, Cloud SQL, or worker-pool service is provisioned.

Credentials are generated on the VM in `/opt/weaves/runtime.env`, readable only
by root. Organization onboarding is token-gated for initialization and disabled
after the owner is created. Local development data and provider credentials are
not copied to the VM. Add your own model provider key in AI & Models to test real
AI responses. OAuth connectors require their own registered callback URLs and
configuration; no company OAuth credentials are reused by this deployment.

## Provision and update

`provision.sh` creates the dedicated network, IAP firewall, VM and snapshot policy.
It assumes billing and Compute Engine/IAP/OS Login APIs have been enabled. All
cloud commands specify the personal project and account explicitly.

Place backend source at `/opt/weaves/backend` and frontend at
`/opt/weaves/weaves-app`, excluding Git metadata, local environment files and
caches. Run `sudo bash /opt/weaves/backend/infra/gcp/personal/activate.sh` on the
VM. It builds both images, creates/verifies the owner, closes onboarding and
starts the worker. Updates preserve the PostgreSQL volume and existing secrets.
Use `/opt/weaves/backup.sh` before updates; inspect `docker compose logs` after
updates. Automatic logical dumps run at 20:15 UTC daily and snapshots at 20:00.

## Stop and resume

Use the personal account/project flags for all commands:

```
gcloud compute instances stop weaves-personal --zone=asia-south1-a --project=weaves-510819 --account=shivamsawarn15@gmail.com
gcloud compute instances start weaves-personal --zone=asia-south1-a --project=weaves-510819 --account=shivamsawarn15@gmail.com
```

Disk and snapshot charges continue while stopped. Docker services restart when
the VM starts. Reopen the tunnel after resuming. Estimated light-use budget is
₹4,000–₹6,000/month before taxes and AI provider charges; this is an allowance,
not a spending cap.

## Restore

Stop the API and worker first. Decompress a selected SQL dump and feed it to
`docker compose --env-file /opt/weaves/runtime.env exec -T postgres psql -U weaves -d weaves`
using a fresh empty database. A full-disk snapshot also restores the runtime
secrets and logical dumps. Preserve the encryption key when restoring database
records with encrypted provider credentials.

## Audit before personal deployment

- Removed retired Knowledge/Memory controls, snapshot fields and capability
  defaults from the frontend; Skills navigation now honors `skills.view`.
- Removed standalone design-preview entrypoints from the shipped frontend image;
  browser tests load those fixtures through test-only routes.
- Excluded local environment files/caches from backend build context.
- Added generated passwords and encryption keys, private same-origin API proxy,
  persistent storage, reduced connection pools, bounded logs and backups.
- Repaired stale backend fixtures for removed permissions, absent seeded
  connectors, current conversation contracts, provenance and cancellation.
- Runtime tests passed: 271 contract/architecture tests. Real PostgreSQL tests
  and model regression checks passed: 29 tests. Further validation results are
  recorded in the task completion report.
- The initial audit found 103 type-check errors and six code-size violations.
  The subsequent cleanup resolved those findings. Strict mypy, Ruff lint and
  formatting, and the existing size limits now pass. See `QUALITY.md` at the
  repository root for commands, scope, and the refactoring details.

## Completed verification

- 271 backend contract/architecture checks passed; all 13 PostgreSQL integration
  tests passed (29 checks including repeated model regression tests).
- 82 browser regression checks passed; six opt-in integration checks were skipped
  in that run. The real deployed owner login, worker task, transcript, exact task
  artifact persistence, authentication gate, disabled onboarding, and logical
  backup were then verified on the GCP VM.
- A browser signed into the real deployed app and opened Overview, Agents,
  Workflows, Skills, Connections, Models and People without API or JavaScript
  errors. Local access is `http://localhost:18080/#signin` while the IAP tunnel
  runs. Owner login details are stored locally in
  `~/.config/weaves-personal/login.json` with mode 0600.
- The deployment smoke script now queues Tasks and verifies worker execution;
  it no longer calls the retired `/runs` or `/artifacts` HTTP endpoints. The VM
  verification additionally checks persisted task artifacts directly in PostgreSQL.
