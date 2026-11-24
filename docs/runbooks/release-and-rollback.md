# Runbook: release and rollback

How releases reach production, how to see what is deployed, and how to roll back safely.
Source of truth: `infra/gcp/singlevm/README.md` ("Automatic deployment"),
`release_ledger.py`, `rollback_release.py`, `release_policy.json` and
`scripts/release_rehearsal.py`. Shell and `dc` helper:
[incident-response.md](incident-response.md) section 0.

## How a release happens (nothing to run)

Merging to `main` in `weaves` or `weaves-app` runs the checks and then automatically
publishes an image **digest** and deploys it to the VM through `apply_release.py`, which
holds one lock so backend and frontend releases cannot race. A backend release first runs
`backup.sh` (a failed backup aborts the release), stops the worker with a 5 minute grace,
starts API and worker (migrations run on API start) and waits for health. If activation
fails the previous image refs and configuration are restored automatically. There is no
staging and no zero-downtime deploy: a release is a short outage. Check a deploy before
pushing the next change.

## See what is running

```sh
sudo python3 /opt/weaves/singlevm/rollback_release.py list                     # whole ledger
sudo python3 /opt/weaves/singlevm/rollback_release.py list --component backend
sudo cat /opt/weaves/singlevm/release_policy.json
```

`list` prints `seq time component result schema revision image` and marks the rollback floor
with `*`. The ledger `/opt/weaves/singlevm/releases.jsonl` is append-only: every attempt,
`healthy` or `failed`, with action `deploy`, `rollback` or `import`. GitHub Actions also
keeps each line as a 90-day `release-manifest` artifact. `releases.json` holds only the
latest image per component.

## The minimum rollback revision (the floor)

`release_policy.json` sets `min_rollback_schema_version` (now 30: migration 30 added the
kill-switch and audit tables) and `min_rollback_revision` (the commit the compatibility
rehearsal builds as the oldest supported old image). The floor is the **first healthy
backend ledger entry whose schema head is at or above that number**. Older entries, and
entries recorded before the ledger existed (schema unknown), are refused as rollback targets,
including by a manual `deploy.py` of the same digest.

**Why a rollback across a migration needs a restore, not a redeploy.** Migrations are
append-only and never reversed, and a migration may remove data (migration 22 purged retired
sign-in rows and indexes). On start every image compares the versions recorded in
`product_schema_migrations` with the versions it knows and refuses to run when the
database holds one it does not know ("Database schema contains unsupported migration
versions"). An image built before migration N therefore cannot start on a database that has
migration N, whatever the ledger says. In practice:

1. A rollback target must have a `schema` value **equal to the current database head** (the
   `schema` column of the newest healthy backend line in `list`). Rolling back across a
   release that added a migration fails at start-up; `apply_release.py` then restores the
   previous image automatically, so the attempt is safe but useless. The floor check does
   not look at the current head, so read the column yourself.
2. Roll **forward** (ship a fix) whenever the target is below the head. This is the default.
3. If you must run code older than the database, the database has to go back too: restore
   the pre-release dump the release took
   ([disaster-recovery.md](disaster-recovery.md) section 2), accepting the data loss since
   then. Never edit `release_policy.json` to widen the floor to make a rollback "work".

The floor is the oldest image the release rehearsal vouches for. Pre-ledger or unknown-schema
entries are never targets.

Raise the floor deliberately: when a new migration breaks older readers, raise
`min_rollback_schema_version` **and** `min_rollback_revision` in the same pull request.

## Roll back

```sh
# 1. choose the target (seq or a unique digest prefix) from `list`, never below the `*`
# 2. dry run: validates against the policy, prints what it would do, changes nothing
sudo python3 /opt/weaves/singlevm/rollback_release.py rollback --component backend --to SEQ --dry-run

# 3. apply: the registry token is read from stdin and is never stored on the VM
gcloud auth print-access-token --account shivamsawarn15@gmail.com | \
  gcloud compute ssh weaves-personal --project weaves-510819 \
  --account shivamsawarn15@gmail.com --zone asia-south1-a --tunnel-through-iap \
  --command 'sudo python3 /opt/weaves/singlevm/rollback_release.py rollback --component backend --to SEQ'
```

Rollback redeploys the ledger digest through the normal locked path: backup, health check,
automatic restore on failure, and a new ledger line with action `rollback`. A target that
never became healthy, is below the floor, or is already running is refused with
`Refused: ...` (exit non-zero). Frontend rollback is the same with `--component frontend`;
frontends have no schema floor. A manual alternative exists
(`python3 infra/gcp/singlevm/deploy.py --component backend --image ...@sha256:DIGEST
--revision COMMIT_SHA` from a tested checkout) but it is subject to the same floor check.

After a rollback: check `/api/v0/health`, watch `dc logs --since 10m product-api`, record the
reason, and open the fix PR. Remember the next merge to `main` deploys again; if the bad
commit is still on `main`, revert it first or the automation re-deploys it.

## Before merging a change that adds a migration

1. `python scripts/release_rehearsal.py run --output release-compat.json` (local throwaway
   Docker stack; builds the floor image with `git archive`, seeds two tenants, upgrades,
   rolls back, rolls forward, checks data, sessions, isolation and Fernet secrets). CI runs
   it and the real-stack suite when `infrastructure/database` changes.
2. `python scripts/release_rehearsal.py verify release-compat.json --revision <sha>`
   (fails closed on a missing, failed, dirty or wrong-revision manifest).
3. Decide whether the floor moves (above). A fresh backup is taken automatically by the
   release.
4. Expand-then-enable: new behaviour behind flags that default off.

## When 6.6 (staging) is approved

Add a staging deploy and smoke test as step 0 of every release and a manual promotion
step; until then there is only the local rehearsal and the PR real-stack harness.
