# Runbook: tenant recovery (one organization lost or damaged data)

**There is no tenant-selective restore tool.** Slice 6.3 (extract one organization from a
backup and import it with conflict rules) is designed but not built, and it depends on
off-host backups (6.1, owner approval pending). Today a backup can be restored whole
([disaster-recovery.md](disaster-recovery.md)), which rolls every tenant back. This runbook
is the safe, manual alternative: freeze, look at the backup in a scratch database, then
repair through supported paths. Shell and `dc` helper:
[incident-response.md](incident-response.md) section 0.

Never: restore a dump over the live database to fix one tenant; copy rows between databases
by hand into production; resurrect a `DELETED` organization (final by design; if the owner
wants a fresh start, create a new organization and re-import from the owner's own export).

## 1. Decide it is a recovery

* Confirm the data is really gone and not just hidden: wrong organization or workspace,
  archived/suspended tenant (`organization_lifecycle.py status`), a deleted agent that the
  owner can restore, or a read-only switch.
* Find out when it happened. The newest on-host dump is at most about 24 hours old (cron
  19:45 UTC) and older dumps exist up to 7 days; snapshots are daily at 20:00 UTC, 7 days.
  Anything newer than the last backup cannot be recovered.

## 2. Freeze the tenant

```sh
cd /opt/weaves/backend/infra/gcp/singlevm
dc() { sudo docker compose -p weaves-single -f /opt/weaves/singlevm/compose.yaml \
  --env-file /opt/weaves/singlevm/runtime.env "$@"; }
ORG=<organization id>
dc exec -T product-api python - quarantine --organization-id "$ORG" --operator you@example.com \
  --reason "data recovery" --ticket REC-5 < organization_lifecycle.py
```

This stops further changes while you work and is reversible with `release-quarantine`.

## 3. Open the backup in a scratch database (read-only investigation)

`check_backup.sh` already restores into a disposable database and drops it afterwards; do the
same by hand to keep it for inspection. The scratch database lives in the live PostgreSQL
instance but is a different database, so production data is not touched. Mind disk space
(`df -h /`; the database is small today but not free).

```sh
DUMP=$(ls -1t /opt/weaves/backups/singlevm/weaves-*.sql.gz | head -3)   # pick the right one
echo "$DUMP"
D=/opt/weaves/backups/singlevm/weaves-YYYYMMDDTHHMMSSZ.sql.gz
SCRATCH=recover_$(date -u +%Y%m%d%H%M%S)
dc exec -T postgres createdb -U weaves "$SCRATCH"
sudo gzip -dc "$D" | dc exec -T postgres psql -v ON_ERROR_STOP=1 -q -U weaves -d "$SCRATCH"
```

Compare what the organization had then and has now (counts only; no content is printed):

```sh
for DB in "$SCRATCH" weaves; do
  echo "== $DB"
  dc exec -T postgres psql -U weaves -d "$DB" -c \
    "SELECT collection, count(*) FROM product_records WHERE org_id = '$ORG' GROUP BY 1 ORDER BY 1;"
done
```

Do not run `restore_reconcile.py` against the scratch database; it is for the live database
after a whole-service restore.

## 4. Repair through supported paths

Choose, in this order:

1. **The owner re-creates it.** Give the owner exact record names and counts (from step 3).
   Most agent and workflow definitions can be rebuilt or re-imported by the owner in the app.
2. **Targeted API restore by an engineer.** For a few records, read them in the scratch
   database as the database owner, then create them through the product API as the
   organization's owner (break-glass session via `issue_account_session.py`, owner consent in
   the ticket) so validation, versioning and tenant rules apply. Secrets cannot be copied
   this way and customers must re-enter provider keys.
3. **Whole-service restore** only when every tenant is affected (a bad migration or disk loss).

Credentials, sessions and OAuth tokens found in a backup are never reused: revoked
credentials stay revoked, customers issue new ones.

## 5. Clean up and release

```sh
dc exec -T postgres dropdb -U weaves "$SCRATCH"
dc exec -T product-api python - release-quarantine --organization-id "$ORG" --operator you@example.com \
  --reason "recovery complete" --ticket REC-5 < organization_lifecycle.py
```

Record in the ticket: what was lost, backup used (file name), what was recreated, who
approved. Dumps are plaintext and hold other tenants' data: never copy one off the VM, and
make sure the scratch database is gone.

## When 6.1 / 6.3 are approved and built

* Backups become encrypted and off-host; restoring them needs the age private key held by
  the two key custodians.
* `tenant_extract.py` / `tenant_import.py` (insert-only, dry-run first, org-bound,
  credentials imported revoked) replace steps 3 and 4 and this page must be rewritten around
  them. Until then treat any claim of "tenant restore" as unsupported.
