# Runbook: incident response

Scope: the production single VM `weaves-personal` (project `weaves-510819`, zone
`asia-south1-a`, Compose project `weaves-single`). Nothing here is automatic: **there is
no alerting yet** (slice 6.5 is not approved), so a person notices an incident from a
customer report, a failed deploy or a manual check. Commands below were checked against
the scripts' `--help`; the VM paths come from `infra/gcp/singlevm/README.md` and could not
be verified from code, so run `dc ps` once to confirm them.

## 0. Get a shell and a helper (every runbook starts here)

```sh
# laptop: IAP-only SSH (never change the globally active gcloud account)
gcloud compute ssh weaves-personal --project weaves-510819 \
  --account shivamsawarn15@gmail.com --zone asia-south1-a --tunnel-through-iap

# on the VM
cd /opt/weaves/backend/infra/gcp/singlevm        # operator scripts live here
dc() { sudo docker compose -p weaves-single -f /opt/weaves/singlevm/compose.yaml \
  --env-file /opt/weaves/singlevm/runtime.env "$@"; }
dc ps                                            # postgres, product-api, product-worker, frontend, gateway
```

Operator scripts are streamed into the running API container and use the database owner
connection: `dc exec -T product-api python - <subcommand> < SCRIPT.py`. Never paste a
secret into a command line, a ticket or a chat.

## 1. Triage (first 10 minutes)

1. Write down the time, who reported it, and any `X-Request-Id` the customer saw (every API
   response carries one; error bodies repeat it).
2. Is it up? From anywhere: `curl -sS -o /dev/null -w '%{http_code}\n' https://api.weave-studio.in/api/v0/health`
   (200 means API and database answer). `dc ps` shows restarting or exited containers.
3. Decide the blast radius: one tenant, all tenants, or data integrity. Data integrity or
   a security concern: go to section 4 *before* investigating further.

## 2. Logs: find a request

Logs are one JSON object per line, rotated at 3 x 10 MB per container (about the last day
under light load; older lines are gone). Fields: `ts`, `level`, `service` (`api` or
`worker`), `logger`, `message`, `request_id`, `org_id`, `workspace_id`, `principal`,
`job_id`, `turn_id`, plus `route`, `status`, `duration_ms`, `outcome`, `exc_type`,
`stack`. Secrets, prompts and tokens are redacted by design.

```sh
REQ=<request id from the customer>
dc logs --no-log-prefix --since 24h product-api product-worker | grep "\"request_id\":\"$REQ\""
# all 5xx in the last 30 minutes
dc logs --no-log-prefix --since 30m product-api | grep -E '"status":5[0-9][0-9]'
# everything one tenant did recently (org id from `organization_lifecycle.py list`)
dc logs --no-log-prefix --since 1h product-api product-worker | grep '"org_id":"ORG_ID"'
# crashes and restarts
dc logs --no-log-prefix --since 1h product-worker | grep -E '"level":"(ERROR|CRITICAL)"'
```

The same request id also sits on persisted task turns and run audit events
(`correlation_id`), so a request can be followed from the API line to the worker line.

## 3. Metrics (Docker network only)

`/api/v0/internal/*` is blocked publicly (Caddy answers 404, and the API also refuses
anything with proxy headers), so read it from inside the API container:

```sh
dc exec -T product-api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/api/v0/internal/metrics').read().decode())" \
  | grep -E 'http_request_seconds_count|admission_refused_total|model_call_total|db_pool_in_use'
```

If `WEAVES_METRICS_TOKEN` is set, add an `Authorization: Bearer` header (the value is in
`runtime.env`; do not echo it). The worker's metrics port is **not enabled in production**
(`WEAVES_WORKER_METRICS_PORT` is unset), so queue depth comes from SQL (read-only):

```sh
dc exec -T postgres psql -U weaves -d weaves -c "SELECT collection, payload->>'status' AS status, count(*), round(extract(epoch FROM now()-min(created_at))) AS oldest_s FROM product_records WHERE collection IN ('execution_jobs','task_turns') AND payload->>'status' IN ('queued','running') GROUP BY 1,2 ORDER BY 1,2;"
```

A growing `queued` count with `running` 0 and an old `oldest_s` means the worker is stuck or
paused (check `platform_control.py status` below). A killed turn is retried after about 5
minutes (stale-lease rule); that is normal, not a hang.

## 4. Contain (kill switches)

Details and reversal: [kill-switch-and-quarantine.md](kill-switch-and-quarantine.md).

```sh
dc exec -T product-api python - status < platform_control.py          # what is set now
# platform-wide: refuse every write with 503 (reads and health still work)
dc exec -T product-api python - set --operator you@example.com --reason "incident 42" \
  --ticket INC-42 --read-only true --message "Back in a few minutes." < platform_control.py
# platform-wide: refuse new work only / stop the worker claiming
dc exec -T product-api python - set --operator you@example.com --reason "incident 42" \
  --admissions-paused true --worker-paused true < platform_control.py
# one tenant
dc exec -T product-api python - quarantine --organization-id ORG --operator you@example.com \
  --reason "abuse report 17" < organization_lifecycle.py
```

Switches act within about 5 seconds and need no restart. Every change is written to the
append-only `operator_actions` table (`platform_control.py actions --limit 20`).

## 5. Common causes

| Symptom | Check | Action |
|---|---|---|
| 5xx on every route right after a deploy | `rollback_release.py list` | [release-and-rollback.md](release-and-rollback.md) |
| `503 platform_read_only` | `platform_control.py status` | someone set read-only; `clear` it if the incident is over |
| `429 limit_exceeded` | `WEAVES_LIMITS_MODE` (currently `observe`: logs only); `admissions_paused` | `organization_limits.py show --organization-id ORG`; clear a pause |
| Tasks stay `queued` | SQL above; `worker_paused`; worker container `dc ps` | `dc restart product-worker` (work finishes its current step) |
| Container restart loop | `dc logs --tail 50 product-api` | startup refuses while a restore is unreconciled: [disaster-recovery.md](disaster-recovery.md) |
| Disk full | `df -h /` and `sudo du -sh /var/lib/docker /opt/weaves/backups` | old dumps are deleted after 7 days; do not delete volumes |
| Sign-in broken | Google client settings in `runtime.env`; break-glass below | `dc exec -T product-api python - --email owner@example.com --minutes 60 < issue_account_session.py` (token printed once; use with `X-Organization-Id`) |

## 6. After the incident

1. Clear every switch you set (`clear`), confirm with `status`, and confirm
   `actions --limit 20` lists what you did.
2. Write the timeline (times, request ids, commands, ticket) in the ticket; no tenant
   content, tokens or secrets.
3. If data was lost or a restore was used, run the follow-ups in
   [disaster-recovery.md](disaster-recovery.md) and tell the affected owners.
4. Once slices 6.1 / 6.5 are approved, update this page: alert names replace step 1,
   and off-host backups change the recovery options.
