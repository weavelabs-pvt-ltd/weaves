# Runbook: tenant deletion and erasure request

For a customer organization that asks to leave or asks for its data to be erased. Shell and
`dc` helper: [incident-response.md](incident-response.md) section 0. Scripts run from
`/opt/weaves/backend/infra/gcp/singlevm`. Waitlist-only requests: section 7.

## Timeline the customer is told

1. Export on request (before deletion): the owner gets a secret-free JSON file.
2. Deletion is requested; **7-day grace period** (`WEAVES_ORGANIZATION_DELETION_GRACE_DAYS`,
   default 7). During grace the tenant is read-only except export and cancellation.
3. The worker purges in bounded steps (poll `WEAVES_LIFECYCLE_POLL_SECONDS`, default 30 s);
   a crash or restart resumes safely. State becomes `DELETED`, which is final. Only one
   worker process purges a given organization at a time (a database advisory lock held for
   one bounded pass); another worker skips it and logs `organization purge busy elsewhere`,
   which is normal. The lock is dropped with its connection if a worker dies.
4. Copies in backups age out: on-host dumps are deleted after 7 days and the disk snapshot
   schedule keeps 7 days, so **data of a deleted tenant can remain in backups for up to
   about 7 days (cron 19:45 UTC dump plus 20:00 UTC snapshot) after DELETED**. Those copies
   are never restored without tombstone replay (see section 5). Say exactly this; do not
   promise instant erasure from backups.

## 1. Verify the request

* It must come from the organization's Owner (or the Owner's written authorization). Record
  who asked, how you verified them, and a ticket id. No ticket, no deletion.
* Confirm the organization: `list` shows ids, names and states.

```sh
cd /opt/weaves/backend/infra/gcp/singlevm
dc() { sudo docker compose -p weaves-single -f /opt/weaves/singlevm/compose.yaml \
  --env-file /opt/weaves/singlevm/runtime.env "$@"; }
dc exec -T product-api python - list < organization_lifecycle.py
ORG=<organization id>
dc exec -T product-api python - status --organization-id "$ORG" < organization_lifecycle.py
```

## 2. Export first (offer it, do not skip it)

Export is owner-only and is done by the customer in the app (Organization settings) or with
`POST /api/v0/organization/export`; it is bounded (default 50 MB limit), secret-free JSON and
records an `organization.exported` audit event. Operators do **not** export on a customer's
behalf. Break-glass only with the owner's written consent in the ticket: issue a short
session for the owner's own account (`issue_account_session.py --email OWNER --minutes 30`,
use with `X-Organization-Id`) and let the owner download it; never copy the file to
your own machine.

## 3. Start deletion (grace period)

The owner can do this in the app. Operator path:

```sh
dc exec -T product-api python - request-deletion --organization-id "$ORG" \
  --operator you@example.com --reason "owner erasure request" --ticket ERA-12 < organization_lifecycle.py
```

State becomes `deletion_requested` with a `grace_deadline`. To stop it:
`cancel-deletion` (an operator-requested deletion can only be cancelled by an operator).
Optionally `suspend` or `quarantine` the tenant meanwhile
([kill-switch-and-quarantine.md](kill-switch-and-quarantine.md)).

## 4. Purge

After the grace deadline the worker purges on its own; nothing to run. To skip the remaining
grace (only with the Owner's written consent in the ticket) use the exact organization name:

```sh
dc exec -T product-api python - purge-now --organization-id "$ORG" --confirm-organization-name "EXACT NAME" \
  --operator you@example.com --reason "owner consented to skip grace" --ticket ERA-12 < organization_lifecycle.py
```

Watch it: repeat `status` until `"state": "deleted"` and `"remaining_rows": 0`.

```sh
dc exec -T product-api python - status --organization-id "$ORG" < organization_lifecycle.py
dc logs --no-log-prefix --since 10m product-worker | grep "\"org_id\":\"$ORG\"" | tail
```

What is removed: every tenant record, secrets (provider keys, connector tokens), API
credentials, memberships, usage ledger and rollups. What is kept on purpose: the
`organizations` tombstone row, lifecycle history, the `organization.lifecycle.*` and
`organization.exported` audit events, and the account's organization-creation quota (a
deleted organization still counts toward the account's cap). If `status` stalls, read the
worker log; the purge refuses to finish while its tombstone cannot be written (see
section 5).

Weaves deletes its stored connector tokens, but do not assume it revokes the grant on the
provider's side (GitHub, Slack, Atlassian). Tell the customer to remove the app in the
provider's settings.

## 5. Tombstones and backups (why a restore cannot resurrect them)

Each purge appends hash-chained tombstones (ids, kinds, timestamps only; no names or emails)
to `/opt/weaves/singlevm/tombstones/tombstones.jsonl` before erasing and again before
reaching DELETED. Verify the log is intact and the org is in it:

```sh
sudo tail -n 3 /opt/weaves/singlevm/tombstones/tombstones.jsonl
dc exec -T product-api python -m modules.recovery.tombstones head --dir /var/lib/weaves/tombstones
```

Any restore of an older dump must be followed by `restore_reconcile.py apply`, which replays
these tombstones and re-purges the organization
([disaster-recovery.md](disaster-recovery.md)). **Current limit:** the log is on the VM disk
only. Until off-host shipping (slice 6.1/6.2, owner approval pending) exists, losing the disk
loses the log, and a restore from a snapshot taken before the deletion brings the tenant
back. After any snapshot restore, re-run deletion for every erasure ticket closed since the
snapshot time (section 6 checklist).

## 6. Close the ticket

Checklist: owner verified; export offered or declined; `status` shows `deleted` and
`remaining_rows` 0; tombstone line present; date after which backups no longer hold the data
(deletion date + 8 days); customer told to remove provider app installs; record the list of
deleted org ids and dates, because **that list is the recovery input after a snapshot
restore**. The `operator_actions` rows hold the audit trail
(`platform_control.py actions --organization-id ORG`).

## 7. Waitlist erasure

```sh
dc exec -T product-api python - delete --email a@b.com --operator you@example.com < waitlist.py
```

Erases the entry and every submission. The worker also deletes submissions older than
`WEAVES_WAITLIST_RETENTION_DAYS` (default 365) hourly.

## When 6.1 is approved

Off-host backups add copies you must also age out (retention is an owner decision between a
strict 35 days and monthly keepers) and tombstones get an off-host, append-only copy. Update
sections 4 to 6 with the real retention window and the shipping check.
