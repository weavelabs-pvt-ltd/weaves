# Weaves Platform Backend

Weaves is a modular agent platform that connects organization systems and
context to bounded agents and workflows. This repository contains the
platform API, contracts, runtime, model and plugin gateways, and PostgreSQL
storage adapter.

The dashboard is maintained separately in
[`weavelabs-pvt-ltd/weaves-app`](https://github.com/weavelabs-pvt-ltd/weaves-app).

## Run the backend

Requirements: Docker Compose.

```sh
cp .env.example .env
docker compose up --build
```

The API is at [http://localhost:8001](http://localhost:8001), its interactive
API explorer is at [http://localhost:8001/docs](http://localhost:8001/docs),
and readiness is reported at `/api/v0/health`. PostgreSQL data is kept in the
`weaves-postgres` Docker volume.

To run the frontend, follow the instructions in the frontend repository and
set `WEAVES_API_BASE=http://localhost:8001/api/v0`.

## Model providers

The local assistant uses a deterministic model until a provider is configured.
The API supports OpenAI, Anthropic, Gemini, and OpenAI-compatible endpoints.
Add a provider through `POST /api/v0/model-providers` with its provider type,
display name, and API key. Then call
`POST /api/v0/model-providers/{provider_id}/refresh-models` to validate the
credential and create selectable model profiles. The compatibility endpoint
`POST /api/v0/models/refresh` accepts `provider_id`; it can omit that parameter
when exactly one active external provider is configured. Assign a profile when
you create or update an agent. Provider keys never appear in API responses.

In PostgreSQL mode, set `WEAVES_SECRET_ENCRYPTION_KEY` before configuring a
provider. Generate one with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Keep the key outside source control and back it up securely: losing it makes
saved provider credentials unreadable. Memory-only mode keeps keys in process
memory, so they must be added again after a restart. The `MODEL_API_KEY`
variables remain available for one environment-configured provider. Set
`MODEL_PROVIDER_TYPE` to `openai`, `anthropic`, `gemini`, or `openai_compatible`;
`MODEL_BASE_URL` and `MODEL_NAME` select its endpoint and model. This bootstrap
path creates one provider profile at startup. Use the provider API when an
organization needs multiple providers or profiles.

Set `WEAVES_API_TOKEN` to map `Authorization: Bearer <token>` to the seeded
local developer principal, or configure `WEAVES_API_TOKENS` to bind tokens to
active principals. Health, onboarding, password login, and invitation
acceptance are public entrypoints. Without a configured token, memory-only mode
allows local development access. PostgreSQL mode requires bearer
authentication for protected routes; create the first organization owner
through onboarding and use its returned credential. Human users can enroll a
password, accept admin-created invitations, and sign in to expiring sessions.
Invitation delivery and email identity verification are not integrated.

## Backend flows

- `GET/POST /api/v0/model-providers` and `POST .../{id}/refresh-models` manage
  provider connections and model discovery. Rotate a key with
  `PUT .../{id}/credential`.
- `GET/PATCH /api/v0/model-profiles` expose selectable models and generation
  defaults. Optional `input_cost_per_million_tokens_usd` and
  `output_cost_per_million_tokens_usd` rates make completed agent runs include
  an `estimated_cost_usd` value.
- `GET/POST /api/v0/threads`, `GET/PATCH /api/v0/threads/{id}`, and
  `GET /api/v0/threads/{id}/runs` manage and inspect workspace-scoped agent
  conversations. Pass `thread_id` to `POST /api/v0/runs` to continue one;
  up to eight recent successful turns (bounded to 24,000 characters) are sent
  as conversation context. Closed threads reject new runs.
- `POST /api/v0/runs` accepts an optional `Idempotency-Key` header. A repeated
  key with the same principal and request replays a successful run; reusing it
  for a different request returns `409`.
- `GET /api/v0/audit-events/export` exports filtered audit events as bounded
  NDJSON pages. Follow `X-Next-Cursor` to continue; see
  [`docs/product-runtime-persistence.md`](docs/product-runtime-persistence.md).
- `POST /api/v0/execution-jobs` queues durable PostgreSQL-backed agent or
  workflow runs for the separate worker. Jobs can be inspected, cancelled at
  safe boundaries, and manually retried when the failure is retryable. The
  synchronous `POST /api/v0/runs` endpoint remains available.
- Workflow admins can configure secret-authenticated webhook event triggers
  for published workflows. Triggers enqueue idempotent durable jobs and execute
  using the creator's current permissions. See
  [`docs/product-workflows-v0.md`](docs/product-workflows-v0.md).
- Workflow admins can configure recurring interval schedules for published
  workflows. The PostgreSQL-backed worker queues one idempotent job per due
  occurrence and coalesces missed intervals. See
  [`docs/product-workflows-v0.md`](docs/product-workflows-v0.md).
- Evaluation suites and runner-scored reports can be stored and inspected in
  the API; the server retains the definition snapshot and checks submitted
  scores against persisted run evidence. See
  [`docs/product-evaluations-v0.md`](docs/product-evaluations-v0.md).
- Organization onboarding is limited to three creations per client address per
  hour. PostgreSQL counters are shared across API instances; memory-mode
  counters are process-local. This does not verify email ownership.
- `GET/POST /api/v0/agents` and `GET/PATCH /api/v0/agents/{id}` create agents
  and publish immutable versions. Agent versions include wall-clock,
  context-call, output-token, and optional estimated-cost budgets. Configure an
  optional JSON Schema `output_schema` on create or patch; model responses must
  validate against it and are returned as `structured_output`. Invalid output
  fails the run while retaining provider usage and audit details.
- `GET /api/v0/agent-templates` lists built-in templates. Create a versioned
  agent from one with `POST /api/v0/agent-templates/{template_id}/agents` and
  explicitly select installed capabilities in the request.
- `GET/PATCH /api/v0/plugin-installations` controls the workspace's registered
  capabilities. `GET/POST/DELETE /api/v0/knowledge-documents` manages context
  used by `knowledge.search`. `GET/POST/DELETE /api/v0/memory-items` manages
  explicitly stored organization/workspace memory; set `memory_scopes` when
  creating or updating an agent to control retrieval scope. Items can have an
  `expires_at` timestamp and are selected through a bounded context builder.
  Thread/workflow items require `scope_ref` pointing at an existing thread or
  workflow and are retrieved only for matching runs. Add read-only GitHub, Slack, or Jira installations
  with `POST /api/v0/plugin-installations`, validate them through
  `POST /api/v0/plugin-installations/{id}/healthcheck`, and rotate credentials at
  `PUT /api/v0/plugin-installations/{id}/credential`. GitHub is limited to
  configured repositories, Slack to configured channels, and Jira to
  configured projects.
- `POST /api/v0/mcp/discover` inspects an MCP endpoint. Connect it through
  `POST /api/v0/plugin-installations` with `plugin_id: "mcp"` and an explicit
  `read_only_tool_names` allowlist. Configure exact endpoint hosts in
  `WEAVES_MCP_ALLOWED_HOSTS`. MCP imports accept tools requiring only a string
  `query`; installed tools then use the normal agent capability allowlist,
  schema validation, invocation, and audit path.
- `GET /api/v0/plugin-descriptors` and `GET /api/v0/capabilities` expose the
  registered plugin and capability contracts used to configure agents.
- `POST /api/v0/runs`, `GET /api/v0/runs`, and `GET /api/v0/runs/{id}` execute
  work and inspect run, tool, and artifact records. Propose a Jira comment at
  `POST /api/v0/runs/{id}/actions`; pending approvals and policy are available
  at `GET /api/v0/approvals` and `GET /api/v0/approval-policies`. Resolve a
  request through `POST /api/v0/approvals/{id}/decision`. Audit history is
  available at `GET /api/v0/audit-events`.

The first usable backend path is provider/model selection → versioned agent →
allowlisted context reads → model response → run artifact and audit history.
Local knowledge, GitHub issue/pull-request search, Slack message search, and
Jira issue search are read-only. One Jira comment action is available only
through the approval lifecycle. GitHub fine-grained tokens need Issues and
Pull requests read access to the listed repositories. Slack requires a user
token with `search:read`; search is restricted to configured channel names.
Jira uses an Atlassian Cloud email and API token, with search restricted to
configured project keys. See the [GitHub](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens),
[Slack](https://api.slack.com/methods/search.messages), and
[Jira](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
permission references for credential setup. Assign an agent one or more of
`github.issues.search`, `slack.messages.search`, and `jira.issues.search`.
Connector credentials are stored through the encrypted secret store and
omitted from API responses.
See [the connector setup guide](docs/product-connectors-v0.md) for request
payloads and the complete setup sequence, including approval-backed actions.

The backend supports organization/workspace-scoped users, roles, agents,
installations, runs, and audit records, along with password sessions and
PostgreSQL-backed worker jobs. It is still an evaluation-grade v0 rather than a
production multi-tenant service: onboarding does not verify email ownership,
MFA/SSO and account recovery are absent, browser session cookie handling is not
provided, and deployment isolation/operations are not production-hardened. The
seeded local approval policy permits self-approval; organization/workspace
defaults forbid it. Production deployments must use verified reviewer
identities and preserve that policy.

## Development

Install dependencies with:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

From the repository root, use `pytest`, `ruff check .`, and `mypy` for the quality
harness. Product contracts and JSON schemas are under
`contracts/v1` and `schemas/product/v1`.

To run a repeatable agent smoke/regression suite against the local API, see
[`docs/product-evaluations-v0.md`](docs/product-evaluations-v0.md). The harness
checks output summaries, required citations, tool-call counts, and records
per-run usage and cost estimates.

## Current boundaries

The current runtime is for local v0 evaluation. It supports local knowledge,
GitHub, Slack, Jira, and allowlisted MCP read capabilities; an approval-gated
Jira comment is the only connector write. Durable queueing requires PostgreSQL
and the separate worker. Email verification, MFA/SSO, recovery, workflow loops,
automated job retries, external-write reconciliation, and
production deployment hardening remain follow-up work. See
[`docs/product-runtime-persistence.md`](docs/product-runtime-persistence.md),
[`docs/product-execution-queue.md`](docs/product-execution-queue.md), and
[`docs/product-workflows-v0.md`](docs/product-workflows-v0.md) for detailed
boundaries.
