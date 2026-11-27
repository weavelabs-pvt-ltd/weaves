# Supported capacity (private beta, Phase 6 slice 6.9)

<!-- gate: worker_slots=4 limits_mode=observe max_running_per_org=2 -->

This is the deployment and load Weaves **declares it supports** for the private beta, and
what that declaration is and is not based on. The line above is read by
`scripts/private_beta_gate.py`, which checks it against `infra/gcp/singlevm/compose.yaml`
(and, when given production state, against the running `runtime.env`). Change the numbers and
this line together.

> **Every performance number here was simulated locally, not measured on GCP.** They come
> from [capacity-envelope.md](capacity-envelope.md) (Phase 5 slice 5.6): the real backend
> image with a fake model provider, in Docker on an Apple M2 laptop, three services pinned
> to two CPUs and memory-capped to mimic the VM. CPU-bound figures (requests per second in
> particular) are optimistic for a shared-core `e2-medium`. Nothing was measured on the
> production VM and no live model latency was calibrated. A disposable-VM rerun is blocked on
> staging (slice 6.6, owner approval pending).

## 1. Declared minimum deployment

| Item | Declared minimum | Today (production) |
|---|---|---|
| Topology | one VM; Docker Compose project `weaves-single`; one API process, one worker container, PostgreSQL 16, frontend, Caddy | same |
| Machine | `e2-medium`: 2 shared-core vCPU, 4 GB RAM | `e2-medium`, `asia-south1-a` |
| Disk | 30 GB `pd-balanced`, not auto-deleted with the VM | 30 GB, daily snapshot, 7 days kept |
| Worker slots | 4 (`WEAVES_WORKER_SLOTS`) | 4 (compose default) |
| Running cap per organization | 2 (`WEAVES_LIMIT_MAX_RUNNING_PER_ORG`) | 2 (compose default) |
| Limits mode | `enforce` is required for the isolation claims in section 3 | **`observe`** (log only); switching to `enforce` is pending the owner's decision after reading the production log |
| Database pools | API 4, worker 8 (pool needs slots + 2) | 4 / 8 |
| Row-level security | optional; measured with it on (the stricter case) | off in `compose.yaml` (`WEAVES_DB_APP_ROLE` empty); confirm in `runtime.env` |
| Public signup | closed: account allowlist set, `WEAVES_PUBLIC_REGISTRATION=false` | closed |
| Memory budget | API about 75 MiB, worker about 55 MiB, PostgreSQL 60 to 110 MiB measured; far below 4 GB | not monitored (no alerts) |

Not supported: more than one worker container in production (the organization purge itself
is safe with several worker processes; the rest is not load-tested that way), horizontal scaling, zero-downtime deploys, high availability. A VM
restart or loss stops every tenant.

## 2. Declared private-beta load

These are policy numbers chosen with headroom below the tested points; the owner may change
them. They are not promises about public availability.

| Dimension | Declared limit | Measured basis (simulated locally) |
|---|---|---|
| Active organizations | up to **10** invited organizations | tested to 200 organizations with a few active: claim delay flat at 2.1 to 2.6 s (the 3 s poll), no sequential scans of `product_records` |
| Seats per organization | 25 (`WEAVES_LIMIT_SEATS`) | list endpoints flat at 200 organizations |
| Open browser tabs (polling Home) | at most **20** at once | single API process saturates at about 53 requests/s here (about 80 tabs at 40 requests/min); halved for the shared-core VM gives about 40; declared at half again |
| Tasks per organization | 10,000 (`WEAVES_LIMIT_STORAGE_TASKS`) | a 6,000-task tenant: Home 41 ms p50 / 105 ms p95, a 50-row page 45 ms |
| Queued work | 50 per organization, 500 platform-wide | a full queue of 25 s calls drains in about 10 minutes at running cap 2; 200 organizations queueing at once drained in 9 s of fake work |
| Request rate | 600 per minute per organization, 240 per principal, burst 100 | the per-org limits protect against one tenant; five organizations at their limit would saturate the API process (no global request budget exists) |
| Model calls | one call may run up to 30 s (adapter timeout) | calls longer than that fail with a retryable `model.timeout` |
| AI spend (customers use their own keys) | caps 2,000,000 tokens/day, 40,000,000/month, USD 25/day per organization | policy, enforced only in `enforce` mode |

Tenant-fairness result that the declaration relies on (envelope declared before measuring,
tenant B normal while tenant A is saturated with 25 s calls): at 4 slots and `enforce`, B's
queue delay p95 is 1.2 s (declared limit 15 s), completion p95 6.3 s (limit 30 s), no failed
turns, Home under 0.1 s. At 2 slots it was 18.6 s, outside the envelope. **In `observe` mode
the same four slots do not isolate tenants** (B waited 20 s at p95, and the hot tenant doubled
its throughput), which is why production, while it stays in `observe`, supports a weaker
claim: tenants are logically isolated by the Phase 0-5 controls, but a noisy neighbour can
slow everyone.

## 3. What is and is not claimed today

Claimed (with the production setting `observe`):
* The checked-in controls T0 to T5 and T6-I03 pass in the real-stack harness; per-tenant data
  isolation, quotas that log, kill switches, tenant quarantine, usage accounting and bounded
  queries are shipped.
* Crash recovery: SIGKILL of the worker or API under load produced no duplicate work and no
  double-counted usage; a killed turn is retried after about five minutes.

Not claimed until the owner flips `WEAVES_LIMITS_MODE=enforce` (its own deploy, after
reading the production `observe` log; roll back by setting `observe`):
* Noisy-neighbour protection (queue-delay and completion bounds for a normal tenant).
* Refusal of over-limit tenants (`429 limit_exceeded`).

## 4. Known limits and unmeasured items

* **Not measured on real hardware:** CPU, requests per second, model latency, and the
  effect of the shared-core burst credit on the `e2-medium`.
* **Disk growth and database size per tenant are not measured.** Today's size is not recorded
  here; check it before every invitation round (`df -h /`, `sudo du -sh
  /opt/weaves/backups/singlevm`, and `SELECT pg_size_pretty(pg_database_size('weaves'))`).
  Dumps are kept for 7 days on the same disk as the database, so the disk must hold the
  database plus about eight dumps plus Docker images; resize before the database exceeds
  about 3 GB.
* **Restore time is not measured on a real dataset or a real VM.** The local drill
  (`scripts/dr_drill.py`) restores a tiny database in about ten seconds, which says only
  that the procedure works. See [runbooks/disaster-recovery.md](runbooks/disaster-recovery.md)
  for recovery point and honest limits (up to 24 hours of data loss; no off-host backup).
* The API process is the first ceiling (single `uvicorn` process); there is no global
  request budget, only per-organization and per-principal limits.
* Idle database work grows slowly with the number of organizations (about 5 tuples/s each)
  and is untested beyond 200.
* Task-turn reservations after a crash are released after about 300 s plus one reconcile
  tick (4 hours before the fix noted in the envelope document).
* Provider rate limits (tokens per minute) belong to the customer and are not modelled.
* Alerts do not exist (slice 6.5 pending): a stuck queue, a full disk or a failed backup is
  noticed only by a person.

## 5. When to re-check

Re-measure on a disposable clone of the VM (needs slice 6.6 approval) and update this file
before: raising any limit above the declared numbers; inviting more than 10 organizations;
changing the machine type; going past 200 organizations; or turning on multiple workers.
`python scripts/private_beta_gate.py` flags a mismatch between this file and the deployed
slot count, running cap or limits mode.
