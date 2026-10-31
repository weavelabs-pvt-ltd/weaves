# Agent evaluation harness v0

The backend includes a repeatable smoke and regression harness for agent
definitions. Evaluation suites are validated by the versioned
`contracts.v1` evaluation contracts. They can be run synchronously
with the CLI or submitted to the durable product worker as an asynchronous run.

## Product API storage

Save or replace a workspace evaluation suite with:

```http
POST /api/v0/evaluation-suites
PUT /api/v0/evaluation-suites/{suite_id}
```

Fetch saved definitions with
`GET /api/v0/evaluation-suites` or `GET /api/v0/evaluation-suites/{suite_id}`.
Suite changes require `agents.manage`; suite reads for execution require
`agents.run`. The suite references an active workspace agent.

After a run, the CLI can submit its scored result to
`POST /api/v0/evaluation-reports`. Reports are workspace-scoped, retain the
definition snapshot used by the runner, and can be listed or fetched through
`GET /api/v0/evaluation-reports` and
`GET /api/v0/evaluation-reports/{evaluation_id}`. Report submission requires
`agents.run`; viewing reports requires `artifacts.read`. Referenced runs must be
visible in the same workspace and must belong to the suite's agent.

Evaluation suites and reports use PostgreSQL when configured and in-memory
storage in local development. The API validates report case IDs, suite snapshots,
and run scope, then recomputes the deterministic checks from the persisted run,
artifact, and tool-invocation records before accepting a result. A case without
a run ID can only be submitted as failed. This does not replace an LLM-as-judge
or prove that the test suite itself is a good measure of task quality.

## Durable asynchronous runs

For suites saved through the product API, submit an asynchronous run with a
unique idempotency key:

```http
POST /api/v0/evaluation-suites/{suite_id}/runs
Idempotency-Key: release-check-2026-10-06
```

The endpoint returns an `EvaluationExecution` in `queued` status. Monitor it
through `GET /api/v0/evaluation-runs` or
`GET /api/v0/evaluation-runs/{evaluation_id}`. Request cancellation with
`POST /api/v0/evaluation-runs/{evaluation_id}/cancel`. PostgreSQL-backed API
storage and a running `product-worker` process are required; the API returns
503 for this endpoint when running in memory-only mode.

The worker runs cases sequentially, stores progress after each completed case,
and writes a server-scored report on success. Cancellation takes effect between
cases, so a model call already in progress is allowed to finish. A failed or
interrupted evaluation cannot be retried using the same job; submit a new run
with a new idempotency key to avoid silently repeating paid model calls. The
synchronous CLI remains useful for local development and operator-triggered
checks, but it does not use this durable job lifecycle.

## Run a suite

Start the API, then from the repository root run:

```sh
WEAVES_API_URL=http://localhost:8001 \
WEAVES_API_TOKEN='your-bearer-token' \
python -m scripts.run_evaluations examples/local-smoke-evaluation.json \
  --output evaluation-report.json
```

To fetch a saved suite and persist its report, use:

```sh
python -m scripts.run_evaluations --suite-id engineering-assistant-smoke \
  --persist-report --output evaluation-report.json
```

To persist a report from a local suite file, first save the same suite definition
through the product API. The API rejects reports if that definition changed
while the cases were running.

The bearer token can be omitted only when the local API is running in
memory-only development mode. Each invocation uses a unique evaluation ID and
an idempotency key per case, so rerunning a suite produces fresh runs while
avoiding duplicate submissions if a request is replayed.

## Suite format

```json
{
  "schema_version": 1,
  "suite_id": "engineering-assistant-smoke",
  "agent_id": "engineering-assistant",
  "cases": [
    {
      "case_id": "release-ticket",
      "task": "Summarize Jira issue REL-42 and cite the source.",
      "must_contain": ["release"],
      "must_not_contain": ["password"],
      "required_sources": ["REL-42"],
      "minimum_tool_calls": 1
    }
  ]
}
```

Every case must complete successfully and pass all configured checks. Phrase
checks are case-insensitive substring checks against artifact summaries;
required sources match exact artifact provenance references. The report
includes per-case pass/fail checks, run IDs, elapsed time, token usage, and
estimated cost when available. It does not copy full model outputs or prompts
into the report. The process exits with status 1 when any case fails and status
2 when the suite cannot be loaded or validated.

## Limits

This is an operator/developer regression tool, not an LLM-as-judge system or a
quality guarantee. Substring assertions are intentionally simple and can miss
semantic errors; suites should use stable expected facts and source references.
Runs use the currently published agent version and current provider/connector
configuration. The report records run IDs so the full trace remains inspectable
through the existing API. Calls use the configured model and can incur cost.
