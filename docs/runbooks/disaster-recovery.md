# Runbook: whole-service disaster recovery

## Read this first: what the current backups can and cannot do

| Copy | Where | Age | Notes |
|---|---|---|---|
| Nightly dump (`backup.sh`, cron 19:45 UTC) | `/opt/weaves/backups/singlevm/` **on the VM disk** | up to 24 h; 7 days kept | plaintext gzip SQL, holds tenant data and encrypted secrets |
| Tombstone log | `/opt/weaves/singlevm/tombstones/` **on the VM disk** | live | needed to avoid resurrecting deleted tenants |
| Disk snapshot (policy `weaves-personal-daily`, 20:00 UTC) | same project, GCP | up to 24 h; 7 days kept | crash-consistent, whole disk (database, dumps, `runtime.env`, tombstones) |
| `gs://weaves-510819-migration-backups` | GCS | frozen 2026-11-15 | historical, pre-reset data only; **not a recovery source** for current tenants |

**Consequences, stated plainly:**

* There is **no off-host backup** (slice 6.1 is not approved). The only copy that survives
  loss of the VM *disk* is a snapshot, and only if the snapshot still exists in the same
  project. If the project, the disk and its snapshots are lost together, the data is gone.
* Recovery point: **up to about 24 hours of data can be lost** (everything since the last
  snapshot or dump). Every customer write, deletion and credential revocation in that window
  is lost.
* `runtime.env` (database password, `WEAVES_SECRET_ENCRYPTION_KEY`, session and OAuth
  secrets) is in no backup except the disk snapshot. If it is lost, stored provider keys and
  connector tokens cannot be decrypted ([secrets-rotation.md](secrets-rotation.md)).
* A snapshot also reverts the tombstone log, so a tenant deleted after the snapshot comes
  back. Re-delete it (section 5).
* RTO is not measured on real infrastructure. The local drill (section 6) restores in about
  10 seconds for a tiny dataset; add VM creation, DNS/IP attach and certificate issuance
  yourself and treat a few hours as the honest planning figure until a real drill is run.

Shell and `dc` helper: [incident-response.md](incident-response.md) section 0. Everything
below that creates or changes cloud resources needs the owner's go-ahead at incident time;
this runbook does not pre-approve it.

## 1. Choose the case

| Situation | Go to |
|---|---|
| Containers down, VM and disk fine | `dc up -d` (then incident-response.md) |
| Database damaged or bad migration, VM fine | section 2 (restore a dump on the VM) |
| VM deleted or unbootable, disk intact | section 3 |
| Disk lost or corrupt | section 4 (snapshot) |
| Everything gone (project, disk, snapshots) | no recovery path exists today; tell the owner |

## 2. Restore a dump on the VM (database lost or wrong)

Stop writers, keep a copy of what is there if it still starts, restore into an **empty**
database, reconcile tombstones, then start. This is the sequence the drill executes.

```sh
cd /opt/weaves/singlevm
dc() { sudo docker compose -p weaves-single -f /opt/weaves/singlevm/compose.yaml \
  --env-file /opt/weaves/singlevm/runtime.env "$@"; }
B=/opt/weaves/backups/singlevm/weaves-YYYYMMDDTHHMMSSZ.sql.gz   # newest good one: ls -1t /opt/weaves/backups/singlevm
sudo gzip -t "$B" && ls -l "$B" "$B.tombstone-head"             # sidecar must exist (see --no-head-check below)

dc stop product-api product-worker                              # frontend and gateway can stay up

# optional safety copy of the current (possibly damaged) database; not matched by the 7-day cleanup
dc exec -T postgres pg_dump --no-owner --no-acl -U weaves -d weaves | gzip | sudo tee \
  /opt/weaves/backups/singlevm/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).sql.gz >/dev/null

# 1. mark the restore: API, worker and apply_release.py now refuse to start until it is reconciled
R="dc run --rm --no-deps -T product-api python -"
$R mark-restore --backup "$B" < restore_reconcile.py

# 2. DESTRUCTIVE: replace the database with an empty one, then load the dump
dc exec -T postgres dropdb -U weaves --force weaves
dc exec -T postgres createdb -U weaves weaves
sudo gzip -dc "$B" | dc exec -T postgres psql -v ON_ERROR_STOP=1 -q -U weaves -d weaves

# 3. give every browser session a new secret (reconcile also revokes all account sessions)
sudo python3 - <<'PY'
import re, secrets
path = "/opt/weaves/singlevm/runtime.env"
text = open(path).read()
line = "WEAVES_BROWSER_SESSION_SECRET=" + secrets.token_urlsafe(48)
if re.search(r"(?m)^WEAVES_BROWSER_SESSION_SECRET=", text):
    text = re.sub(r"(?m)^WEAVES_BROWSER_SESSION_SECRET=.*$", line, text)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
open(path, "w").write(text)
PY

# 4. replay tombstones: look first, then apply
$R apply --dry-run --head-file "$B.tombstone-head" < restore_reconcile.py
$R apply --head-file "$B.tombstone-head" < restore_reconcile.py

# 5. start and check
dc up -d product-api product-worker
curl -sS https://api.weave-studio.in/api/v0/health
```

Notes on `restore_reconcile.py` (`--help` checked):

* `apply` replays purges, credential revocations and secret deletions onto the restored
  database, revokes **every** account session and writes the completion marker. It is
  idempotent: rerun it with `--restore-id ID` after an interruption. `status` lists pending
  restores.
* It refuses, changing nothing, when the tombstone log is corrupt, shorter than the backup's
  recorded head, or no head is supplied. Use `--no-head-check` only for a backup that has no
  `.tombstone-head` sidecar; secret-rotation tombstones are then skipped and listed.
* If a `restore-pending-*` file exists without its `restore-reconciled-*` file the services
  exit at start-up by design. Do not delete the marker; finish `apply`.
* Everyone must sign in again. Owners may need to re-issue API credentials that were
  revoked or created after the backup.

## 3. VM gone, disk intact

The disk `weaves-personal` is not auto-deleted with the VM. Recreate the VM on the existing
disk with the same shape as `infra/gcp/personal/provision.sh` (e2-medium, network and subnet
`weaves-personal`, tag `weaves-personal`, **no service account**, OS Login on, project SSH
keys blocked) and the existing reserved regional address (`gcloud compute addresses list`).
Creating the VM is a cloud change: owner approval first.

```sh
G="--project weaves-510819 --account shivamsawarn15@gmail.com --zone asia-south1-a --quiet"
gcloud compute instances create weaves-personal $G --machine-type e2-medium \
  --disk name=weaves-personal,boot=yes,auto-delete=no --network weaves-personal \
  --subnet weaves-personal --tags weaves-personal --no-service-account --no-scopes \
  --metadata enable-oslogin=TRUE,block-project-ssh-keys=TRUE --address ADDRESS_NAME
```

Containers use `restart: unless-stopped` and start with Docker. Then verify with section 7.

## 4. Disk lost: restore from a snapshot

```sh
G="--project weaves-510819 --account shivamsawarn15@gmail.com"
gcloud compute snapshots list $G --filter="sourceDisk~weaves-personal" \
  --sort-by=~creationTimestamp --limit=8 --format="table(name,creationTimestamp,diskSizeGb)"
```

Pick the newest snapshot, note its timestamp (that is the start of the data-loss window),
then (owner approval, creates a disk and a VM):

```sh
gcloud compute disks create weaves-personal-restored $G --zone asia-south1-a \
  --source-snapshot SNAPSHOT_NAME --type pd-balanced
gcloud compute instances create weaves-personal $G --zone asia-south1-a --quiet \
  --machine-type e2-medium --disk name=weaves-personal-restored,boot=yes,auto-delete=no \
  --network weaves-personal --subnet weaves-personal --tags weaves-personal \
  --no-service-account --no-scopes \
  --metadata enable-oslogin=TRUE,block-project-ssh-keys=TRUE --address ADDRESS_NAME
```

If the old VM still exists, stop and delete only that VM (keep disks) first. Re-attach the
snapshot schedule to the new disk (`gcloud compute disks add-resource-policies
weaves-personal-restored --resource-policies weaves-personal-daily --zone asia-south1-a
$G`). The database is crash-consistent: PostgreSQL replays its WAL on start, so check
`dc logs postgres` for recovery messages. Because the snapshot is the whole disk, database,
`runtime.env` and tombstones are from the same moment and no `restore_reconcile.py` run is
needed (a *dump* restore needs it, a disk snapshot does not).

## 5. After any restore

1. Health: `curl -sS https://api.weave-studio.in/api/v0/health`, then sign in as the owner.
   If Google sign-in fails, use the break-glass session in incident-response.md.
2. **Re-run deletions** for every erasure ticket closed since the restore point
   ([tenant-deletion-and-erasure.md](tenant-deletion-and-erasure.md) section 6 list):
   `request-deletion` then `purge-now` with the owner's recorded consent.
3. Re-apply anything in `operator_actions` you can still see in tickets: quarantines,
   suspensions, limit overrides, switches (`platform_control.py status`).
4. Tell affected customers the data-loss window (snapshot or dump time to incident time).
5. Check the release ledger: `sudo python3 /opt/weaves/singlevm/rollback_release.py list`.
   The running image must be one the database schema is compatible with.
6. Write the postmortem: measured time to recovery, window of lost data, what slowed you down.

## 6. The drill (local only, safe to run any time)

`scripts/dr_drill.py` runs this whole procedure on a **local throwaway Docker stack**: own
PostgreSQL, API and worker on a private network, synthetic tenants, a generated key.

```sh
python scripts/dr_drill.py --output dr-drill.json
```

It seeds three tenants (records, workflows, encrypted secrets), backs up like `backup.sh`
(tombstone head, `pg_dump`, `gzip -t`), writes one record after the backup, deletes one
tenant, destroys the database, then recovers with `mark-restore`, restore, `apply --dry-run`,
`apply`, start. It asserts that survivors read back their backup-time data and isolation
holds, the deleted tenant stays deleted, every pre-disaster session is refused, the
post-backup write is lost (the data-loss window is real), a secret does not decrypt with
another key, and API and worker stay up. It refuses to start if the environment names a
production project, host or database, if the Docker daemon is remote, if
`/opt/weaves/singlevm/runtime.env` exists, or if the image comes from the production
registry repository. What it does **not** prove: VM or disk creation, snapshots, DNS,
certificates, a real dataset size, or off-host recovery. Run it monthly; the private-beta
gate (`scripts/private_beta_gate.py`) wants evidence younger than 30 days.

## 7. Verify a recovered service

```sh
dc ps
curl -sS https://api.weave-studio.in/api/v0/health
dc exec -T product-api python - status < platform_control.py      # no stale read-only switch
dc exec -T product-api python - list < organization_lifecycle.py   # expected organizations and states
sudo python3 /opt/weaves/singlevm/rollback_release.py list --component backend
```

## What changes when 6.1, 6.2 shipping and 6.6 are approved

* Backups are encrypted and uploaded write-only to a private bucket; a lost VM or disk
  becomes recoverable from off-host data (recovery point shrinks to the upload interval);
  the age private key (two custodians) and an escrow of `runtime.env` become prerequisites.
* Tombstones are shipped off-host, so a snapshot or dump restore replays them and section 5
  step 2 is no longer manual.
* A staging VM makes a timed, real restore drill possible (replace section 6's caveats with
  measured numbers). Until then this page describes a procedure that has only been rehearsed
  locally.
