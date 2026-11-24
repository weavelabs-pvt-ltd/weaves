# Beta capacity envelope (Phase 5 slice 5.6)

> **Simulated locally with container limits, not measured on GCP.** Every number
> here comes from the real backend image (API, native worker, PostgreSQL 16)
> running in Docker on an Apple M2 laptop, with the three services that share the
> production VM pinned to two CPUs and given memory caps, and a fake model
> provider. Nothing was run against production and no cloud resource was created.
> Treat CPU-bound numbers (the API ceiling in particular) as optimistic for a
> shared-core `e2-medium` and re-check them on a disposable clone of the VM before
> raising any limit that depends on them. Phase 6 owns that rehearsal.

The measurements answer the Phase 5 exit gate: a written beta capacity envelope
with enforced tenant caps, chosen from results rather than from a promised tenant
count. Reproduce with `python3 -m tenancy_harness.phase5.load_run` (opt-in, about
two hours, refused in CI; see `tenancy_harness/README.md`).

## 1. What was simulated

| Aspect | Production (`infra/gcp/singlevm`) | This run |
|---|---|---|
| Machine | `e2-medium`: 2 shared-core vCPU, 4 GB, one VM runs everything | Docker Desktop VM, 8 CPUs, 4 GB, Apple M2 (arm64) |
| CPU for PostgreSQL + API + worker | 2 vCPU shared with the OS, frontend and Caddy | `cpuset 0,1` for all three; fake provider and load generator on CPUs 2 to 5 |
| Memory caps | 4 GB host | PostgreSQL 1280 MiB, API 768 MiB, worker 1024 MiB (about 3 GiB, the rest left to the frontend, Caddy and the kernel) |
| Database | `postgres:16-alpine`, default settings, disk volume | same image and settings, named volume (not tmpfs) |
| Processes | one `uvicorn` process, one worker container | same |
| Pools and HTTP | API pool 4, worker pool 8, 20 HTTP connections | same |
| Worker poll | 3 s | 3 s |
| Row-level security | optional (`WEAVES_DB_APP_ROLE`, dormant by default) | on (`weaves_app`), the stricter case |
| Model provider | the customer's real provider | fake, `tenancy-slow=N` holds a call for N seconds |
| Model call durations | 3 to 30 s (client timeout 30 s) | noisy neighbour: tenant A 25 s, tenant B 5 s; restart audit: instant, 1 s and 15 s |

Why these numbers are optimistic: an M2 core very likely runs this Python code
two or more times faster than a burstable `e2-medium` vCPU (an estimate, not
measured), and an `e2-medium` is documented as sustaining one vCPU of work, not
two. The memory numbers transfer (they are
process sizes); the latency and requests-per-second numbers do not.

No live model run was made: calibrating real latency needs paid calls and the
fake provider already covers 3 to 30 s, so the lifetime budget was not touched.

## 2. The service envelope declared before measuring

Committed in `tenancy_harness/phase5/noisy_neighbor.py` (`ENVELOPE`) before the
first measured run, and never moved afterwards. Tenant A is saturated (its queue
held at 45 of the 50 cap, every call 25 s, the slowest the 30 s client timeout
allows); tenant B is a normal tenant submitting one task every 20 s (3 per minute,
5 s model calls) while its owner keeps Home open (one request per 1.5 s).

| Bound for tenant B | Declared |
|---|---|
| Queue delay (created to started) p95 | at most 15 s |
| Queue delay maximum | at most 45 s |
| Completion latency (created to finished) p95 | at most 30 s |
| Failed turns | 0 |
| API latency p95 (Home, task list) | at most 1 s |
| HTTP errors | 0 |

The slow call was first set to 30 s and changed to 25 s before any measured run,
because every provider adapter times out at 30 s (section 6).
A stress profile (B every 6 s, 10 per minute, 5 s calls) is reported next to it
and is not part of the declaration. The first smoke run showed B at 10 per minute
cannot be served by two slots beside a saturated tenant; the normal profile was
set to 3 per minute before any measured run.

## 3. Noisy neighbour: results

Tenant A saturated, tenant B normal or stressed. 300 s measured window after a
30 s warm-up; every row is one run. Delays are seconds from the task turn row
(`created_at` to `started_at`), not from the client.

| Slots | Mode | Run cap per org | B submits every | B queue delay p50 / p95 / max (s) | B completion p95 (s) | B done per min | A done per min | Within envelope |
|---|---|---|---|---|---|---|---|---|
| 1 | enforce | 2 | 20 s | 118.2 / 162.0 / 169.1 | 167.1 | 2.0 | 2.0 | **no** |
| 1 | observe | 2 | 20 s | 93.0 / 147.0 / 154.1 | 152.1 | 2.0 | 2.0 | **no** |
| 2 | enforce | 2 | 20 s | 5.9 / 18.6 / 20.4 | 23.7 | 3.2 | 4.2 | **no** |
| 2 | observe | 2 | 20 s | 6.2 / 18.9 / 20.7 | 24.0 | 3.2 | 4.2 | **no** |
| 4 | enforce | 2 | 20 s | 0.7 / 1.2 / 1.2 | 6.3 | 3.0 | 4.6 | yes |
| 4 | observe | 2 | 20 s | 7.7 / 20.2 / 20.4 | 25.3 | 3.2 | 8.8 | **no** |
| 2 | enforce | 1 | 20 s | 1.3 / 2.8 / 2.9 | 7.9 | 3.0 | 2.4 | yes |
| 2 | enforce | 2 | 6 s | 210.3 / 264.4 / 269.0 | 269.5 | 4.2 | 4.0 | **no** |
| 4 | enforce | 2 | 6 s | 1.3 / 2.7 / 2.8 | 7.8 | 10.0 | 4.8 | yes |
| 2 | enforce | 1 | 6 s | 1.5 / 2.8 / 3.0 | 7.9 | 10.2 | 2.4 | yes |

Reading the table:

* **One slot cannot serve a second tenant** (p95 147 to 162 s): the slot alternates
  a 25 s call with a 5 s call, so B gets 2 per minute against 3 arriving.
* **Fair claiming is fair per claim, not per second.** With two slots and a running
  cap of two, A's two 25 s calls keep both slots busy; B is claimed every time a
  slot frees (about every 12 s), so its p95 is 18.6 s (19.2 s in the replicate): close to, but over, the
  declared 15 s. At the stress rate it is overloaded (4.2 per minute served of 10
  arriving, delay still growing at 264 s).
* **Four slots with the running cap enforced meets the envelope with room**: B p95
  1.2 s (1.1 s) at 3 per minute and 2.7 s (2.6 s) at 10 per minute (all 10 per minute completed),
  while A still gets its capped share (4.6 to 4.8 per minute, which is
  2 slots x 60 / 25 s).
* **`observe` does not isolate.** At four slots in `observe` mode A took all four
  slots (its throughput doubled to 8.8 per minute) and B waited 20 s at p95. The
  cap is only enforced in `enforce`, so raising slots without `enforce` makes the
  hot tenant faster without protecting anyone.
* **A running cap of one with two slots also meets the envelope** (p95 2.8 s) but
  halves the hot tenant's throughput (2.4 per minute). It is the fallback if the
  VM cannot take four slots.
* Home stayed responsive in every run: `/overview` and `/tasks` p95 between 0.07
  and 0.10 s for both tenants, task creation p95 0.06 to 0.10 s, zero HTTP errors
  and zero failed turns (the stack answered 202 for every submission).
* Database connections peaked at 8 (default limit 100); the API pool peaked at 2
  of 4 and the worker pool at 1 of 8 in the 5 s samples. Memory: API 72 MiB,
  worker 54 MiB, PostgreSQL 55 to 78 MiB, far below the caps.

Replicate run with a steady-state CPU window (docker stats samples inside the
measured window only, so tenant provisioning does not inflate the peaks):

| Slots | Mode | Cap | B every | B delay p50 / p95 / max (s) | B done / min | CPU % of one core, mean (p95): API | worker | PostgreSQL | Memory peak MiB: API / worker / PG | DB conns peak | In envelope |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2 | enforce | 2 | 20 s | 7.3 / 19.2 / 22.2 | 3.2 | 2.6 (7.8) | 0.9 (3.9) | 10.6 (25.5) | 72.4 / 52.9 / 61.0 | 7 | **no** |
| 4 | enforce | 2 | 20 s | 0.6 / 1.1 / 1.1 | 3.0 | 2.5 (5.9) | 1.0 (3.9) | 11.4 (21.6) | 73.5 / 53.9 / 66.0 | 8 | yes |
| 2 | enforce | 1 | 20 s | 1.6 / 2.9 / 3.0 | 3.0 | 2.1 (4.3) | 0.7 (3.5) | 8.8 (15.9) | 72.3 / 53.4 / 64.3 | 7 | yes |
| 4 | observe | 2 | 20 s | 7.7 / 17.8 / 21.9 | 3.2 | 2.2 (4.5) | 0.9 (4.4) | 10.5 (19.0) | 72.4 / 53.7 / 69.2 | 8 | **no** |
| 4 | enforce | 2 | 6 s | 1.2 / 2.6 / 2.9 | 10.2 | 1.8 (3.9) | 0.9 (3.0) | 9.5 (18.8) | 72.8 / 54.2 / 76.3 | 8 | yes |

* Resource use is nowhere near a limit at these rates (7 to 14 tasks a minute plus
  two polling tabs): in the measured windows the API averaged 2 to 3 % of a core
  (p95 4 to 8 %), the worker about 1 % (p95 3 to 4 %) and PostgreSQL 9 to 11 %
  (p95 16 to 26 %). That is roughly 0.1 CPU-second of worker per task, so the
  worker's CPU would cap throughput near ten tasks a second; model-call wait
  and the slot count cap it far sooner. The fake provider returns instantly and
  small, so real responses add some CPU per call; this is the number to
  re-measure on the real VM.
* The replicate agrees with the first run within a second everywhere, including
  which cells meet the envelope.

## 4. Tenant growth, large histories and API ceiling

Organizations were added in stages with the system otherwise idle. Each has its
own agent and finished work. At each stage: the database work the idle API and
worker generate (from `pg_stat_database` and `pg_stat_user_tables` over 60 s), how
long a probe tenant waits to be claimed, the time for one task in every
organization to drain, and endpoint latency for the probe tenant.

| Organizations | Rows in `product_records` | Idle tuples read / s | Seq scans of records / s | Probe claim delay p50 / p95 (s) | One task in every org drains in (s) | `/overview` p50 / p95 (ms) | `/tasks` p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| 10 | 403 | 158 | 0.0 | 2.2 / 2.4 | 3.4 | 28 / 36 | 23 / 33 |
| 50 | 2138 | 202 | 0.0 | 2.2 / 2.6 | 4.3 | 27 / 41 | 25 / 33 |
| 100 | 4824 | 667 | 0.0 | 2.1 / 2.3 | 4.5 | 30 / 38 | 23 / 33 |
| 200 | 9578 | 994 | 0.0 | 2.3 / 2.5 | 9.4 | 28 / 48 | 26 / 34 |

One tenant with 6000 tasks (200 other organizations present): keyset walk 120 pages, largest 50 rows, 5.5 s (2 waits for the request limiter); idle tuples read 837 / s, seq scans 0.0 / s, claim delay p95 2.3 s.

| Endpoint, tenant with the large history | p50 / p95 (ms) | Statuses |
|---|---|---|
| `/overview?tz_offset_minutes=0` | 41 / 105 | {'200': 20} |
| `/tasks?limit=50` | 42 / 49 | {'200': 20} |
| `/tasks?paginate=true&limit=50` | 45 / 51 | {'200': 20} |
| `/agents` | 36 / 48 | {'200': 20} |
| `/users` | 41 / 66 | {'200': 20} |
| `/usage` | 30 / 46 | {'200': 20} |

API ceiling (closed loop, Home polling across 200 tenants, single `uvicorn` process):

| Client threads | Requests / s | Latency p50 / p95 (ms) | Statuses |
|---|---|---|---|
| 2 | 54.0 | 33 / 61 | {'200': 1622} |
| 4 | 52.8 | 72 / 101 | {'200': 1587} |
| 8 | 53.7 | 149 / 202 | {'200': 1614} |
| 16 | 52.3 | 298 / 448 | {'200': 1576} |

Container peaks over the whole phase (dominated by the API ceiling step): api CPU peak 103.9 % (p95 36.7 %), memory peak 77.0 MiB, postgres CPU peak 102.3 % (p95 95.8 %), memory peak 112.2 MiB, worker CPU peak 97.4 % (p95 5.0 %), memory peak 53.9 MiB.

* Claim delay is flat (2.1 to 2.6 s, almost all of it the 3 s poll) from 10 to 200
  organizations, with 200 organizations queueing work at the same moment draining
  in 9 s.
* There was never a sequential scan of `product_records` (0 per second at every
  stage), including with a 6,000-task history.
* Idle database work is **not flat but small**: it rises with the number of
  organizations (about 5 tuples per second per organization, 1,000 per second at
  200); the cause was not investigated. No bound is promised beyond the
  tested 200 organizations; re-measure before going past it.
* Home (`/overview`) for the 6,000-task tenant: p50 41 ms, p95 105 ms; a 50-row
  page 45 ms; a full keyset walk of 6,000 tasks is 120 pages of at most 50 in
  5.5 s (the request limiter slowed it twice).
* **API ceiling: about 53 requests per second** of Home polling for the single API
  process regardless of client concurrency (2 to 16 threads), after which latency
  grows linearly (p95 61 ms at 2 threads, 448 ms at 16). That is about 3,200
  requests a minute, or 80 open tabs at the 40 requests per minute a polling tab
  issues; allow for half of that on an `e2-medium` (about 40 tabs).

## 5. Restart and crash behaviour

Two organizations submitted about 190 tasks (a third instant, a third 1 s, a
third 15 s model calls) with a stable `Idempotency-Key` through six outages of the
real containers: SIGKILL of the worker, SIGKILL of the API, SIGTERM (graceful,
30 s) of the worker, SIGKILL of both, then two more worker SIGKILLs. There are no
test hooks, so kills land wherever the processes are: under this load that
includes claim, usage reservation, the provider call and settlement. The audit
reads the database and the provider's own call count.

Kill schedule (seconds after the stream began, each restarted 4 s later): 45 s SIGKILL worker; 75 s SIGKILL API; 105 s SIGTERM worker (30 s grace); 135 s SIGKILL API and worker; 165 s SIGKILL worker; 195 s SIGKILL worker.

| Invariant | Held |
|---|---|
| recovered within wait | yes |
| provider calls never exceed reservations | yes |
| every settled row reached the provider | yes |
| new work accepted after recovery | yes |
| new work runs after recovery | yes |
| one task per idempotency key | yes |
| one turn per task | yes |
| every turn terminal | yes |
| attempts within the retry bound | yes |
| at most one run per turn | yes |
| open reservations only for failed turns | yes |
| ledger keys unique | yes |
| every succeeded run settled with equal tokens | yes |
| every settled row is exactly one reported call | yes |
| rollup equals ledger | yes |
| unknown never counts tokens | yes |

Per organization (both identical): 94 tasks accepted, turns {'succeeded': 92, 'failed': 2}, attempts {'1': 92, '2': 2}, by call length {'fast/succeeded': 37, 'slow1/succeeded': 38, 'slow15/failed': 2, 'slow15/succeeded': 17}, ledger {'settled/ok': 92, 'reserved/None': 2}, open reservations 2 holding 4664 tokens, refused at the queue cap [429]. Provider calls 188 = ledger rows 188 (settled 184). Seconds from the end of the stream until nothing was queued or running: 306.3.

What this shows, and what it does not:

* **No duplicate work, no double counting.** One task and one turn per
  idempotency key, at most one agent run per turn, one provider call per
  reservation (188 calls, 188 ledger rows), every succeeded run settled with the
  provider-reported tokens exactly once, the daily rollup equal to the ledger, and
  unknown or open reservations never counted tokens.
* **No leaked capacity.** After recovery nothing was queued or running, both
  organizations could submit and finish new work, and capacity is counted from
  rows, so there is no counter to leak.
* **Kills cannot be attributed one by one.** The schedule includes a graceful
  SIGTERM, which the worker answers by finishing in-flight work, but the audit only
  sees the totals: four turns (two per organization, all 15 s calls, the longest
  exposed) failed, and 184 of 188 provider calls were settled.
* **Crash recovery takes about five minutes.** A turn whose worker was killed is
  requeued by the existing 300 s stale-lease rule; it was idle after 306 s, and
  the four affected turns (two per organization) failed on their second attempt
  with the retryable `task.agent_run_failed`.
* **During a worker outage the queue cap refuses**, as designed: each organization
  had one submission answered 429 (queue at 50) for the full 90 s client retry and
  created nothing.
* **Known gap (reconciliation of killed task turns).** A killed turn's usage
  reservation is not released at the lease fence; the reconciler's
  `STALE_RUN_AFTER_SECONDS` rule releases it after four hours. Until then the
  reserved tokens (2,332 each in this run) count against the organization's daily
  token cap. T5-I03 proves the 120 s fence only for execution jobs. It is bounded
  and self-healing, so it does not block the beta, but it should be fixed by
  making the reconciler consult the task turn's lease.

## 6. Findings that changed the code in this slice

1. **Long model calls failed under row-level security.** The task-turn and
   execution-job heartbeat threads started with an empty tenant context, so under
   `WEAVES_DB_APP_ROLE=weaves_app` their lease update matched no row, the lease
   looked lost and the run was cancelled: every task turn over the 15 s heartbeat
   interval failed (16 s and 20 s calls failed, 14 s passed), and the job lease
   renewal silently stopped. The role is dormant until `WEAVES_DB_APP_ROLE` is set
   (`infra/gcp/singlevm/README.md`), which is why earlier fast tests never saw it;
   enabling RLS in production would have broken every real model call over 15 s. Both
   threads now run in a copy of the claiming thread's context
   (`tests/contracts/test_heartbeat_tenant_context.py` fails without the fix).
2. The client timeout for every provider adapter is 30 s, so a call longer than
   that fails with `model.timeout` (retryable). The tests use 25 s as the slow
   call for that reason.
3. The queue-age warning (`WEAVES_QUEUE_AGE_WARN_SECONDS`) is global, so a tenant
   legitimately at its queue cap (measured 313 to 434 s of oldest queued age)
   would trip a 120 s threshold continuously (section 7).

## 7. Recommended production values

All values are per organization unless noted; every environment variable is read
by `shared/limits_config.py`.

| Setting | Recommended | Basis |
|---|---|---|
| `WEAVES_LIMITS_MODE` | `enforce` for the beta, after one read of the production `observe` log (see section 8) | Measured: only `enforce` applies the running cap; at four slots `observe` leaves B at 20 s p95 instead of 1.2 s. Code default stays `observe` (safe for a fresh environment). |
| `WEAVES_WORKER_SLOTS` | 4 (production compose changed from 2) | Measured: B p95 1.2 s at four slots vs 18.6 s at two, memory unchanged (54 MiB), pool 8 sufficient (needs slots + 2). Real calls are network bound. |
| `WEAVES_LIMIT_MAX_RUNNING_PER_ORG` | 2 (1 if slots must stay at 2) | Half the slots. Measured cap 1 at two slots: B p95 2.8 s, A at 2.4 per minute. |
| `WEAVES_LIMIT_MAX_QUEUED_PER_ORG` | 50 | Policy plus a drain bound: 50 calls of 25 s at running cap 2 drain in about 10 minutes. Claim cost did not grow with queued organizations. |
| `WEAVES_LIMIT_GLOBAL_MAX_QUEUED` | 500 | Ten full organization queues; 200 organizations queueing at once drained in 9 s of fake work. At four slots 500 calls of 10 s take about 20 minutes. |
| `WEAVES_ADMISSIONS_PAUSED` | `false` | Operator switch, not capacity. |
| `WEAVES_LIMIT_REQUESTS_PER_MINUTE` (org / principal), burst | 600 / 240, 100 | Kept. They protect against one tenant; the single API process serves only about 3,200 requests a minute in total (about 1,600 on an `e2-medium`), so five organizations at their limit would saturate it. A global request budget is a Phase 6 item. |
| `WEAVES_LIMIT_SEATS` | 25 | Product policy; list endpoints were flat at 200 organizations. |
| `WEAVES_LIMIT_SCHEDULES` | 20 | Policy; each schedule is one ordinary queued job. |
| `WEAVES_LIMIT_AGENTS` / `WEAVES_LIMIT_WORKFLOWS` | 50 / 50 | Policy; `/agents` p95 below 65 ms. |
| `WEAVES_LIMIT_TOOL_CALLS_PER_DAY` | 2,000 | Policy; not capacity bound (a tenant at the running cap makes at most about 17,000 calls a day). |
| `WEAVES_LIMIT_TOKENS_PER_DAY` / `_PER_MONTH` | 2,000,000 / 40,000,000 | Policy; reaches its limit long before the worker does. |
| `WEAVES_LIMIT_EST_COST_USD_PER_DAY` | 25 | Policy. |
| `WEAVES_LIMIT_STORAGE_TASKS` | 10,000 | Measured to 6,000 tasks (Home 41 ms p50); plans bounded by 5.4 indexes. |
| `WEAVES_USAGE_RESERVATION_INPUT_CHARS_PER_TOKEN` | 3 | Unchanged; calibrating it needs a live provider (not run). |
| `WEAVES_QUEUE_AGE_WARN_SECONDS` | 900 (code default changed from 120) | A saturated tenant waits 313 to 434 s; 900 s still flags a stuck worker. |
| `WEAVES_PAGE_DEFAULT` / `WEAVES_PAGE_MAX` | 50 / 100 | Measured: a page is 45 ms and 6,000 tasks walk in 5.5 s. |
| `WEAVES_RECOVERY_BATCH` | 200 | Unchanged; no sequential scans observed, recovery after a worker kill took 306 s (the lease), not batch bound. |
| `WEAVES_USAGE_RETENTION_DAYS` | 400 | Policy. |
| Database pool (API / worker) | 4 / 8 | Measured connections peaked at 8 of 100 with four slots. |

## 8. Decisions

* **Raise slots to four: yes.** It is what lets B meet the envelope with the cap
  enforced. The compose default is changed to 4 and the code default was already 4.
* **Switch `WEAVES_LIMITS_MODE` to `enforce`: yes for the beta, as its own
  deploy after reading the production `observe` log.** The measurements support
  it, the harness already runs in `enforce`, and without it four slots
  help the noisy tenant. What these local runs cannot show is whether real
  tenants would have been refused by the queue, request, seat or token limits;
  that is the production `observe` log. Do not flip it without looking at the
  log, and keep the rollback (`WEAVES_LIMITS_MODE=observe`) one line.
* **Declared envelope for a normal tenant (B).** At four slots, `enforce`, running
  cap 2 and one other tenant saturating its queue with 25 s calls: queue delay
  p95 at most 15 s and maximum at most 45 s (measured 1.2 s and 1.2 s at 3 tasks
  a minute, 2.7 s and 2.8 s at 10 a minute), completion p95 at most 30 s
  (measured 6.3 s and 7.8 s), no failed turns, Home and the task list under 1 s at
  p95 (measured 0.1 s). The saturated tenant itself waits as long as its own
  queue takes to drain (about ten minutes for a full queue of 25 s calls) and is
  never refused except at the queue cap.
* **Not promised:** a number of tenants. The tested point is 200 organizations
  with a few active; the ceiling that matters first is the API process at about
  53 requests a second here (about 40 polling tabs on the real VM).

## 9. Known limits

* **One VM, one worker container, one API process.** Any restart or VM loss
  stops all tenants; a killed running turn waits about five minutes (the stale
  lease) before it is retried. There is no horizontal scaling and no capacity for
  two worker containers without fixing the race below.
* **Organization purge race with more than one worker process** (PR #52):
  the purge tick's two read-modify-write steps on the lifecycle record are not
  locked, so two worker processes can lose an update of `clean_ticks`. One
  container runs today. A fix would wrap them in `guard.mutation()`.
* **The API process is the first ceiling** (about 53 requests per second here),
  and there is no global request budget, only per-organization and per-principal
  limits.
* **CPU numbers are from faster hardware** (see the banner). **Model latency was
  not calibrated live.**
* **Idle database work grows with the organization count** (linear, small) and is
  untested beyond 200 organizations.
* **Task-turn reservations after a crash are released after four hours**
  (section 5).
* The restart audit is statistical: kills land at random points of the request
  path. Both organizations ran the same schedule and produced identical counts.
* Provider rate limits (tokens per minute) are the customer's own and are not
  modelled; a provider 429 is a retryable model error.
