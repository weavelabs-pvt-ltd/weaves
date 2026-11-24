# Runbook: kill switches and tenant quarantine

Use when work must stop quickly: abuse, a runaway tenant, a security concern, a bad deploy
that corrupts data, or a provider incident. Everything here is reversible except
`--revoke-credentials`. Set up the shell and `dc` helper from
[incident-response.md](incident-response.md) section 0 first. All commands run on the VM
from `/opt/weaves/backend/infra/gcp/singlevm`, are never reachable over HTTP, and write one
row to the append-only `operator_actions` table (operator, reason, ticket, correlation id).

## Pick the smallest switch that works

| Need | Command | Effect (within about 5 s, no restart) |
|---|---|---|
| Stop new work, keep everything else | `platform_control.py set --admissions-paused true` | new tasks, runs, webhook and schedule enqueues get `429 limit_exceeded` (resource `admissions_paused`) in every limits mode |
| Stop the worker taking work | `platform_control.py set --worker-paused true` | no new jobs or turns are claimed; running work finishes; queued work waits |
| Freeze all writes | `platform_control.py set --read-only true --message "..."` | every POST/PUT/PATCH/DELETE and webhook gets `503 platform_read_only`; reads, health and CORS preflight work |
| Close new account creation | `platform_control.py set --signup-enabled false` | Google sign-in cannot create accounts (`signup_closed`); existing accounts sign in |
| One tenant | `organization_lifecycle.py quarantine` | see below |
| One tenant, hard stop | `... suspend` | all of that tenant's requests get `403 Organization not available` |

```sh
cd /opt/weaves/backend/infra/gcp/singlevm
dc() { sudo docker compose -p weaves-single -f /opt/weaves/singlevm/compose.yaml \
  --env-file /opt/weaves/singlevm/runtime.env "$@"; }

# state
dc exec -T product-api python - status < platform_control.py
dc exec -T product-api python - actions --limit 20 < platform_control.py

# set (give a ticket); any combination of --admissions-paused / --worker-paused /
# --read-only / --signup-enabled, each true|false
dc exec -T product-api python - set --operator you@example.com \
  --reason "runaway tenant" --ticket INC-42 --admissions-paused true < platform_control.py

# release: clear everything, or only named switches
dc exec -T product-api python - clear --operator you@example.com \
  --reason "incident over" --ticket INC-42 < platform_control.py
dc exec -T product-api python - clear --operator you@example.com --reason "resume work" \
  --switch worker_paused < platform_control.py
```

A switch that was never set (or was cleared) follows its environment default
(`WEAVES_ADMISSIONS_PAUSED`, default false). If the database cannot be read a process keeps
the last value it read; an explicit read-only stays on. `status` shows stored overrides and
the effective values.

## Quarantine one tenant

```sh
ORG=<organization id>        # from: dc exec -T product-api python - list < organization_lifecycle.py
dc exec -T product-api python - quarantine --organization-id "$ORG" \
  --operator you@example.com --reason "abuse report 17" --ticket INC-43 < organization_lifecycle.py
dc exec -T product-api python - quarantine-status --organization-id "$ORG" < organization_lifecycle.py
```

Quarantine suspends the organization (its API credentials and sessions get `403`, the
worker cancels its work at the next boundary, schedule and webhook dispatch skip it),
disables its active schedules and webhook triggers, and records exactly what it changed.
Other organizations are untouched. Account sessions are **not** revoked because the same
person may belong to other organizations. Connector secrets are kept, unreachable while
suspended. Repeating the command is a no-op.

Release it:

```sh
dc exec -T product-api python - release-quarantine --organization-id "$ORG" \
  --operator you@example.com --reason "cleared by review" --ticket INC-43 < organization_lifecycle.py
```

Release re-enables only the schedules and triggers quarantine disabled (and only if they are
still disabled), resumes the organization only if quarantine suspended it (an earlier
operator suspension stays), and never replays missed work.

### Irreversible: revoke credentials

`quarantine ... --revoke-credentials` also revokes the organization's API credentials. It
writes a tombstone first (so a restore cannot revive them) and release does **not** bring
them back; the customer must issue new ones. Use it only when a credential is believed
leaked.

## Plain suspend / resume

```sh
dc exec -T product-api python - suspend --organization-id "$ORG" --operator you@example.com \
  --reason "billing hold" < organization_lifecycle.py
dc exec -T product-api python - resume  --organization-id "$ORG" --operator you@example.com \
  --reason "paid" < organization_lifecycle.py
```

Only an operator can resume a suspension; the customer's owner cannot. Resume never replays
missed schedules, cancelled jobs or old webhook events.

## Checks and exit codes

* `0` ok; `2` refused (unknown organization, disallowed transition; a refusal is audited);
  `3` the action applied but its audit row could not be written: fix the cause and record the
  action by hand in the ticket.
* After any change run `status` / `quarantine-status` and look at `actions --limit 5`.
* Tested continuously by harness control `T6-I03` (quarantine, each switch timed under 9 s,
  other tenants unaffected, audit rows append-only).

## Not covered today

There is no automatic trigger and no paging: nothing tells you to use these. Slice 6.5
(alerts) is awaiting owner approval; when it lands, add the alert-to-switch mapping here.
