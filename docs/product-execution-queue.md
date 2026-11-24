# Product Execution Queue

## Local flow

`POST /api/v0/execution-jobs` accepts a task targeting either an agent or a
published workflow and stores a durable execution job. Omit both IDs to use the
seeded assistant or the first active agent in the selected workspace. An
optional `Idempotency-Key` makes acceptance replay-safe for the same principal
and workspace. `GET /api/v0/execution-jobs` and
`GET /api/v0/execution-jobs/{job_id}` show queued, running, cancel-requested,
succeeded, failed, or cancelled state. `POST /api/v0/execution-jobs/{job_id}/cancel`
cancels queued work immediately or requests safe-boundary cancellation from a
running worker. `POST /api/v0/execution-jobs/{job_id}/retry` queues a
new job for a retryable failure; it requires an `Idempotency-Key` and records
the original job ID as retry lineage. The existing `POST /api/v0/runs` endpoint
remains synchronous.

The `product-worker` Compose service polls PostgreSQL and claims one queued job
with a single `FOR UPDATE SKIP LOCKED` update. It calls the same agent runtime
as synchronous runs, using the job ID as the run idempotency key, then stores
the linked run ID and terminal job state. Queue transitions and audit records
are committed together. Workflow jobs dispatch through the selected published
workflow version. The API returns 503 for queue submission when it is using
in-memory storage because another process cannot see that queue.

## Fair claiming and worker slots (Phase 5.2)

By default (`WEAVES_FAIR_CLAIM=true`) the worker no longer claims the globally
oldest row. Each claim serves organizations round robin (least recently claimed
first, recorded in `tenant_scheduler_state`) and takes that organization's oldest
queued job or task turn with `FOR UPDATE SKIP LOCKED`. Under
`WEAVES_LIMITS_MODE=enforce` an organization already running
`WEAVES_LIMIT_MAX_RUNNING_PER_ORG` rows (jobs and turns together, counted from
the rows, never from a stored counter) is skipped; under `observe` the claim
proceeds and the would-be deferral is logged. A per-organization advisory lock
makes the count-then-claim step race free across slots and worker processes.

`WEAVES_WORKER_SLOTS` threads each claim and run one unit of work. The main thread
is the supervisor: recovery, schedule dispatch, studio, purges, the queue-age
warning (`WEAVES_QUEUE_AGE_WARN_SECONDS`) and claim-state pruning keep running
while executions are in flight, and a slot thread that dies is restarted. A failing
slot backs off exponentially up to 30 seconds. On SIGTERM no new work is claimed
and every in-flight execution finishes before the process exits; a killed process
leaves its rows `running` until the existing lease recovery fails them, which
frees the organization's capacity. The worker database pool defaults to
`slots * 2 + 4`; slots are reduced when an explicit pool is smaller than
`slots + 2`. `WEAVES_WORKER_SLOTS=1 WEAVES_FAIR_CLAIM=false` restores the original
single loop.

Rollout is staged: production runs `WEAVES_WORKER_SLOTS=2` (pool 8) in
`infra/gcp/singlevm/compose.yaml`; the code default of 4 (pool 12) is raised in
production only after the slice 5.6 measurements, and 1 slot with
`WEAVES_FAIR_CLAIM=false` remains the rollback.

Workflow jobs persist the published workflow version selected when the request
is accepted. Later edits do not change the queued request's agent sequence;
retries preserve that same version. Revoking the workflow or disabling an agent
still prevents execution when the worker starts.

The worker also dispatches due recurring workflow schedules. It checks every
five seconds by default; set `WEAVES_SCHEDULE_POLL_SECONDS` to a value from
0.5 through 60 to change the cadence. Schedule occurrences become ordinary
durable workflow jobs and are processed through the same queue.

Saved evaluation suites use this same worker queue through
`POST /api/v0/evaluation-suites/{suite_id}/runs`. Their progress is available
through `GET /api/v0/evaluation-runs` and
`GET /api/v0/evaluation-runs/{evaluation_id}`. A worker runs cases
sequentially and persists each result. If it stops after all results were
saved, recovery creates the report and marks the job succeeded. If it stops
with cases still missing, recovery marks the evaluation failed and
non-retryable, because repeating completed model calls could incur cost. A
cancel request is finalized as cancelled if the worker stops. Evaluation jobs
cannot use the generic execution-job retry endpoint; submit a new evaluation
with a new idempotency key instead. Cancellation takes effect between cases,
after any active model call returns. This flow requires PostgreSQL and the
`product-worker`, just like other durable queue work.

Start the API, PostgreSQL, and worker with:

```sh
docker compose up --build
```

## Delivery behavior and limits

- The queue is PostgreSQL-backed; in-memory repositories support unit tests,
  not multi-process queue operation.
- A worker claims a job once. Jobs do not retry automatically. A failed job
  keeps its linked run when one exists; users can submit a new request after
  reviewing a non-retryable failure. Requesters and workspace administrators
  can explicitly retry retryable failures. Each retry is a new job and the
  original remains failed; repeating the same retry key returns that job.
- A requester or workspace administrator can cancel a queued job or request
  cancellation from a running worker. Queued cancellation uses a conditional
  state update so it cannot overwrite a concurrent worker claim. Running jobs
  enter `cancel_requested`; the worker continues renewing its lease. Standalone
  agent jobs observe cancellation between context calls and before model
  execution; workflows observe it between steps. During a parallel group, every
  already-started branch must return before the worker observes cancellation
  and stops before the next step. An in-flight external call cannot be
  interrupted. If cancellation arrives during the final call and execution
  completes before another safe boundary, the run and job finish successfully.
  If the worker dies after a cancellation request, stale recovery finalizes the
  job as cancelled.
- The worker renews a running job's lease every 20 seconds. Heartbeats and
  terminal transitions are fenced by both worker ID and claim attempt, so an
  expired or recovered worker cannot overwrite the current job state. The API
  and worker reconcile jobs without a heartbeat for two minutes; recovery marks
  the job succeeded if its linked run completed, otherwise failed with an audit
  event. The worker's 30-second maintenance cadence means recovery may occur up
  to roughly 30 seconds after that threshold.
- A job's execution envelope contains the task text. Access is limited to its
  requester and principals with `users.manage` in the job's workspace.
- Run state transitions are transactional in PostgreSQL, but external model and
  connector effects cannot join those transactions. For example, a connector
  write with an uncertain response still needs connector-level idempotency and
  reconciliation.
- A lost worker cannot be interrupted mid-call. Its external model or connector
  request may finish after the job lease is lost; only the currently fenced
  worker can persist the job's terminal transition. Connector-side idempotency
  and reconciliation remain necessary for uncertain external effects.
- This v0 worker has no automated backoff/retries, per-customer worker
  isolation, or production queue metrics. API and worker clocks must remain
  synchronized for lease expiry decisions.
